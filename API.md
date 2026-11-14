# Local API contract

## Xác thực và phân quyền

`POST /api/login` nhận `{"username":"admin","password":"..."}`, đặt cookie `HttpOnly; SameSite=Strict` và trả `csrf`. Mọi POST sau đó gửi `X-CSRF-Token`.

- `GET /api/me`, `POST /api/logout`: quản lý phiên.
- `GET/POST /api/users`: chỉ `admin`.
- `GET /api/traces`: chỉ `admin`, trả 100 trace gần nhất.
- `GET /api/catalog`, `POST /api/ask`: mọi vai trò đã đăng nhập.
- `/api/ask` trả thêm `trace_id`, `cache_hit`, `mode` và `model_route`.
- Nếu LLM local đã sửa truy vấn, phản hồi có `spelling_corrected: true` và `normalized_question`; trace ghi span `model.ollama.normalize_query`.

Mã chính: `401 AUTH_REQUIRED`, `403` sai CSRF/thiếu quyền, `400` đầu vào sai.

Input được kiểm tra bằng NeMo Guardrails `self check input` qua Ollama trước khi vào RAG. Chính sách nằm tại `guardrails_config/config.yml`; ứng dụng không dùng danh sách keyword để nhận diện prompt injection.

Loopback only: `http://127.0.0.1:8765`. No remote publication.

* `GET /`: HTML interface có màn hình đăng nhập.
* `GET /api/catalog`: `{products: string[], records: integer, model: string}`.
  `products` không chứa phạm vi nội bộ `STORE-POLICY`.
* `POST /api/ask`: cookie phiên và header `X-CSRF-Token`; JSON
  `{question: string (1..2000 characters), product: string, use_model: boolean}`.
  `use_model` defaults to true.

200 response: `{status, reason, answer, citations, mock, mode}`.

* `evidence`: the local model selected evidence, displayed verbatim.
* `search_results`: lexical retrieval only; not a verified answer.
* `handoff`: no factual answer; no actual ticket or notification is created.

Each citation is a complete approved record with `id`, `product`, `topic`,
`text`, `source`, `valid_from`, `valid_to`, `approved`, `mock`, optional
`keywords`, `fact_key`, `fact_value`.
Validation: 400; missing session token or invalid Host: 403; unknown route: 404;
body >64,000 bytes: 413; another question being processed: 429; unexpected
server error: 500. Model failure returns 200 with `status=handoff` and
`reason=model_unavailable`.

## Ollama dependency

Only `POST http://127.0.0.1:11434/api/chat` is used.
Request: `model`, `messages`, `stream:false`, `format` JSON schema,
`options.temperature:0`. Expected completed response:
`{done:true, message:{content:"<JSON>"}}`.
Content schema: `{ids: string[], insufficient: boolean, conflict: boolean}`.
Unknown IDs, malformed results, timeout and unavailable model fail closed.
Generated prose never enters the final response.

Reference checked: https://docs.ollama.com/api/chat
No hosted provider discovery is required for this local service.
No customer or mock documents are uploaded to Postman.

## Chat update — 2026-11-05

POST /api/ask also accepts optional `history`: at most 6 objects with
`product: string` and `question: string` (1..2000 characters). Invalid history
returns 400. The body limit is now 64,000 bytes to allow bounded history.
History is supplied by the browser and is untrusted input, not evidence.
The server only uses the most recent same-SKU question for explicit references
such as “chính sách đó”. Independent questions are not augmented.

Responses also contain `context_used: boolean`. New status `conversation`
is used for a small list of greetings/thanks, without model calls or citations.
Existing statuses and requests without history remain supported.

Retrieval always searches the selected product plus `STORE-POLICY`. This lets
store-wide questions such as shipping, address, opening hours and payment work
for every SKU without mixing another product's specifications.
The browser clears its in-memory history on SKU change, Chat mới or reload.

Retrieval now splits questions on “và”, “cũng như”, semicolon/question mark,
and removes introductory phrases such as “chính sách”. Each clause needs a
matching candidate and, with the model enabled, matching selected evidence.
This is a lexical heuristic, not full natural-language understanding.


## Conversations & orchestrator — 2026-11-10

- `GET /api/conversations`: 20 cuộc trò chuyện gần nhất của người dùng hiện tại.
- `GET /api/conversations/<id>`: `{conversation, messages}`; mỗi tin assistant có `status`, `citations`, `ai_generated`.
- `POST /api/conversations` `{product}` → 201 cuộc trò chuyện mới.
- `POST /api/conversations/<id>/delete` → `{ok: true}`; 404 nếu không thuộc người dùng.
- `POST /api/ask` nhận thêm `conversation_id`. Có id: server dùng lịch sử đã lưu (bỏ qua `history`).
  Không có id: server tạo cuộc trò chuyện mới và trả `conversation_id`. Id lạ/của người khác → 404.
- Trạng thái mới: `model_answer` (AI trả lời ngoài RAG, `ai_generated: true`, `disclaimer`, không citation)
  và `out_of_scope`. Lý do handoff mới: `authoritative_source_required`, `needs_human`.
- Phản hồi có thêm `model_tier`: `light` | `heavy` | `cloud` | null.
- `GET /api/catalog` → `routing` = `{mode, cloud_enabled, general_answer, models}`.
