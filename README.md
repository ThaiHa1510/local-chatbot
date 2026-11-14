# Trợ lý CSKH điện tử chạy local

## Bảo mật, cache, quan sát và chọn mô hình

- Ba vai trò: `viewer` chỉ chat, `agent` thao tác ticket, `admin` quản lý người dùng, xem trace và thực thi gửi thông báo.
- Lần chạy đầu, tài khoản `admin` và mật khẩu dùng một lần được in trong Terminal. Có thể đặt trước bằng `SUPPORT_ADMIN_USERNAME` và `SUPPORT_ADMIN_PASSWORD`.
- NVIDIA NeMo Guardrails dùng input self-check với model Ollama `qwen3.8:27b` để phát hiện prompt injection trước RAG và tool, thay cho danh sách keyword/regex tự viết. Quyền công cụ vẫn do RBAC trong mã quyết định.
- Khi truy xuất câu gốc không đủ, Ollama sửa lỗi chính tả, câu không dấu, viết tắt và dịch ý định đa ngôn ngữ sang tiếng Việt rồi tìm lại. Kết quả chuẩn hóa chỉ dùng làm truy vấn, không được dùng làm câu trả lời hay sự thật.
- Câu trả lời đủ bằng chứng được cache 15 phút, tách theo SKU, đường model và phiên bản kho. Handoff và nội dung bị chặn không được cache.
- Mỗi lần RAG/model/MCP tool sinh trace, thời gian và eval. Trace chỉ lưu hash/kích thước đầu vào tại `data/traces.jsonl`.

Mặc định mọi tác vụ dùng Ollama. Để bật hybrid có chủ đích:

```bash
export MODEL_ROUTING_MODE=hybrid
export ALLOW_CLOUD_MODEL=true
export OPENAI_API_KEY='...'
export OPENAI_COMPLEX_MODEL='gpt-6-astra'
.venv/bin/python app.py --port 8876 --model Qwen:latest
```

Khi cloud chưa bật hoặc thiếu khóa, tác vụ phức tạp tự hạ xuống Ollama. Khi bật cloud, câu hỏi phức tạp và tài liệu ứng viên được gửi tới OpenAI Responses API bằng Structured Outputs và `store:false`.

Bản mẫu chạy trong `.venv` Python 3.12 với Ollama và NVIDIA NeMo Guardrails.
Tất cả sản phẩm và chính sách đều **GIẢ LẬP**, không đại diện cho hãng thật.

Dự án có thêm MCP server cho ticket và liên hệ nhân viên. Xem `MCP_GUIDE.md`.
Mặc định thông báo chạy ở chế độ console, chưa gửi email thật.

## Chạy ứng dụng

Mở Terminal trong thư mục này:

```sh
uv sync --python 3.12
.venv/bin/python import_catalog.py --all --missing-only   # tạo bảng giá từ data/sources/
.venv/bin/python app.py --port 8876
```

Mở http://127.0.0.1:8876. Trên Mac có thể mở `start.command` (tự làm cả ba bước trên, dùng thêm
`--heavy-model qwen3.8:27b`). Mặc định dùng mô hình `Qwen:latest`. Ollama phải đang chạy.
Trên máy khác, chọn tên mô hình đã cài:

```sh
.venv/bin/python app.py --model TEN_MO_HINH_LOCAL
```

### Mang sang máy khác

1. Ở máy này mở `pack.command` → tạo `dist/LocalSupportAgent-YYYYMMDD.zip`.
   Gói có mã nguồn, tài liệu, `data/sources/` (dữ liệu crawl), `data/mock_knowledge.json`,
   `data/MOCK/`, `data/business_profile.json`. **Không** kèm `.venv`, bảng giá đã sinh, tài khoản,
   lịch sử chat, phiếu hỗ trợ, cache, trace (xem `.gitignore`).
2. Máy mới cần: macOS/Linux, [uv](https://docs.astral.sh/uv/), Ollama và các model
   (`ollama pull qwen3.8:27b` cùng model nhẹ bạn chọn).
3. Giải nén, mở `start.command` (hoặc chạy ba lệnh ở trên). Lần đầu app tự tạo DB trống và
   in **tài khoản admin + mật khẩu dùng một lần** ra Terminal.
4. Hạn giá = ngày crawl trong nguồn + 14 ngày. Hết hạn thì bot chuyển nhân viên thay vì báo giá cũ.
   Chỉ để demo với dữ liệu cũ: `.venv/bin/python import_catalog.py --all --from-today`.

Bỏ chọn “Kiểm tra bằng mô hình local” để chỉ tra cứu, không cần Ollama.
Dừng bằng Ctrl+C. Không mở cổng ứng dụng ra mạng công cộng.

## Dữ liệu mẫu

41 đoạn tài liệu: laptop LAP-A14, điện thoại PHONE-P6, tai nghe AUDIO-B2,
màn hình MON-M27 và mã DEMO-CONFLICT để thử nguồn mâu thuẫn.

Trong đó có 11 đoạn `STORE-POLICY` dùng chung: giao hàng và phí giao, kiểm hàng,
địa chỉ/giờ mở cửa giả lập, liên hệ, thanh toán, đặt hàng, hóa đơn VAT, bảo mật
thông tin và thay đổi/hủy đơn. `STORE-POLICY` không hiện như một sản phẩm để chọn;
chatbot tự tìm chính sách này cùng tài liệu SKU đang chọn.

Các cách hỏi vị trí được nhận diện gồm “danh sách cửa hàng”, “chi nhánh”,
“cửa hàng gần đây/gần nhất”, “hệ thống cửa hàng” và “địa chỉ cửa hàng”. Vì dữ
liệu mẫu hiện có một địa chỉ, kết quả chỉ liệt kê một địa điểm giả lập. Chatbot
chưa đọc vị trí GPS; câu “gần đây” chỉ trả danh sách nguồn, không tính khoảng cách.

Mỗi sản phẩm có thông số, thời hạn/điều kiện bảo hành, đổi trả, thủ tục và
hướng dẫn xử lý lỗi. Có thêm tài liệu hết hạn và bản nháp để kiểm thử lọc.
Nguồn gốc đọc được nằm trong `data/MOCK/`; dữ liệu dùng để nhập là
`data/mock_knowledge.json`. Bản SQLite được tạo tự động khi chạy lần đầu.

Thử:

* LAP-A14: “Sản phẩm được bảo hành bao lâu?”
* PHONE-P6: “Máy có hỗ trợ eSIM không?”
* MON-M27: “Tần số quét qua HDMI là bao nhiêu?”
* AUDIO-B2: “Điều kiện đổi trả là gì?”
* PHONE-P6: “Giá hôm nay là bao nhiêu?” → thiếu dữ liệu.
* DEMO-CONFLICT: “Bảo hành bao lâu?” → nguồn mâu thuẫn.

## Luồng RAG

Câu hỏi + SKU → lọc nguồn đã duyệt và còn hiệu lực → BM25 tìm tối đa
5 đoạn → kiểm tra mâu thuẫn bằng khóa thông tin → mô hình local chọn ID
bằng chứng hoặc từ chối → ứng dụng hiển thị nguyên văn đoạn nguồn.

Đây là RAG với đầu ra bị giới hạn vào bằng chứng, chưa phải chatbot diễn đạt
tự do. Mô hình chỉ sinh danh sách ID và quyết định đủ/thiếu thông tin.
Cách làm này ngăn chèn thông số do mô hình tự viết, nhưng **không bảo đảm
nguồn đúng, chọn đúng đoạn, hoặc đủ ngữ cảnh**. Người quản trị phải duyệt nguồn.

Không có embedding/vector search trong bản này. BM25 hoạt động theo từ khóa,
có chuẩn hóa dấu tiếng Việt; ngưỡng trùng từ là heuristic, không phải xác suất
đúng. Những câu hỏi dùng từ đồng nghĩa có thể bị từ chối.
Hội thoại được lưu trên server (xem mục “Orchestrator, nạp lại ngữ cảnh và trả lời ngoài RAG”).

Mâu thuẫn được chặn chắc chắn khi các bản ghi cùng `fact_key` có
`fact_value` khác nhau. Mâu thuẫn trong văn xuôi phụ thuộc mô hình và chưa được
bảo đảm phát hiện. Giữ nguyên cả điều kiện và ngoại lệ trong một đoạn.

## Hỏi giá, so sánh, lọc sản phẩm (`catalog.py`)

Câu hỏi như “laptop nào giá rẻ nhất”, “điện thoại dưới 10 triệu”, “so sánh A14 và B15”,
“laptop RAM 16GB rẻ nhất”, “giá bao nhiêu” được trả lời bằng **truy vấn có cấu trúc**, không qua RAG văn bản:

1. Bảng giá: `data/catalog.json` cộng mọi file `data/catalog_*.json` (nhập bằng `import_catalog.py`, xem dưới).
   Dữ liệu giả lập dùng cho test nằm ở `tests/fixtures/catalog_mock.json`. Mỗi dòng có `sku, category
   (laptop|phone|monitor|audio), name, aliases, price_vnd, specs (ram_gb, storage_gb, screen_inch, weight_kg),
   status (selling|discontinued), source, valid_from, valid_to, approved, mock`. File sai định dạng thì app dừng khi khởi động.
2. Luật trong `catalog.parse()` đọc câu hỏi (có dấu hoặc không dấu) thành bộ lọc: loại sản phẩm, tiêu chí “nhất”,
   khoảng giá (“dưới 15tr”, “10-20 triệu”, “tầm 1 triệu”), RAM/bộ nhớ tối thiểu, mã sản phẩm được nhắc tới.
3. Chỉ khi luật không hiểu (ví dụ “máy tính xách tay hợp túi tiền sinh viên”), model nhẹ được nhờ trả bộ lọc JSON
   theo schema có enum. Model **không nhận giá và không viết câu trả lời**.
4. Python lọc trên dòng đã duyệt, còn hiệu lực, đang bán; sắp xếp; ghép câu trả lời. Citation là chính các dòng bảng giá.
5. Hai giá còn hiệu lực cho cùng SKU → chuyển nhân viên. Bảng giá hết hạn → chuyển nhân viên.
   Khuyến mãi, tồn kho vẫn luôn cần nhân viên xác nhận. Câu về bảo hành, đổi trả, giao hàng, phí… vẫn đi luồng RAG như cũ.

Nhập bảng giá thật (bỏ dòng không giá, đặt hạn giá, sửa đơn vị bộ nhớ, tách máy để bàn, thêm alias):

File nguồn đặt ở `data/sources/<tên>.json` (hiện có `phones`, `laptops`, `audio`) và sinh ra
`data/catalog_<tên>.json`:

```bash
.venv/bin/python import_catalog.py --all                       # nhập lại tất cả
.venv/bin/python import_catalog.py data/sources/phones.json --name phones --days 14   # một file
```

Bảng giá đã sinh không cần mang theo; muốn thêm nhóm hàng mới chỉ cần thả file vào `data/sources/`.

Audio được tách thành tai nghe / loa / micro–máy ghi âm theo tên; hãng tự suy ra từ tên khi nguồn để `null`.

Lọc theo từ khóa trong tên và CPU/chip/màn: “laptop gaming rẻ nhất”, “laptop OLED dưới 30 triệu”,
“laptop core i7”, “ryzen 7”, “core ultra 5”, “macbook m5”, “điện thoại snapdragon”, “intel”/“amd”.
Tai nghe/loa: “tai nghe chống ồn”, “chụp tai”, “bluetooth/không dây”, “có dây”, “thể thao”, “gaming”,
“loa karaoke”, “loa chống nước”.
Rồi khởi động lại app.
Cache câu trả lời tự mất hiệu lực khi file thay đổi.

### Model viết truy vấn SQL (`catalog_sql.py`)

Câu hỏi bảng giá “mở” (“laptop nào có giá thành trên hiệu năng tốt nhất”, “laptop gaming đáng tiền nhất
dưới 30 triệu”, “có bao nhiêu mẫu macbook”) được model local viết thành **một câu SQLite SELECT**:

- Model chỉ thấy mô tả cột của bảng `products` tạm trong bộ nhớ (chỉ hàng đã duyệt, còn hiệu lực), không thấy giá.
- SQLite authorizer chỉ cho SELECT, đọc `products`, hàm trong danh sách cho phép; chặn ghi, PRAGMA, ATTACH,
  bảng hệ thống, CTE đệ quy, nhiều câu lệnh; truy vấn quá nặng bị ngắt; tối đa 10 dòng.
- Câu hỏi nhạy cảm (giá nhập, lợi nhuận, thông tin khách hàng, mật khẩu…) bị chặn trước khi tới model.
- SQL được chạy thử ngay khi gọi model; hỏng thì thử tầng model khác, rồi quay về luật.
- Câu trả lời do Python ghép từ dòng kết quả; “Mình hiểu câu hỏi là…” không được chứa số tiền không có trong câu hỏi.
- Thiếu dữ liệu (pin dạng số, card đồ họa…) → nói thật “bảng giá chưa có dữ liệu”, không trả danh sách theo giá.
- `perf_score`/`value_score` là **điểm ước tính** từ dòng CPU, số lõi, RAM, SSD — câu trả lời luôn ghi rõ.

Model nhỏ viết SQL không đủ tin cậy; dùng model lớn cho tầng heavy:
`.venv/bin/python app.py --port 8876 --heavy-model qwen3.8:27b` (start.command đã đặt sẵn).
Tên máy cụ thể (“giá iphone 17 pro”) vẫn tra bằng luật alias vì chính xác và nhanh hơn.

## Thay dữ liệu

Tạo JSON theo cấu trúc mẫu, tối đa 5.000 ký tự/đoạn. Các trường bắt buộc:
`id`, `product`, `topic`, `text`, `source`, `valid_from`, `valid_to`,
`approved` (boolean), `mock` (boolean). Ngày theo YYYY-MM-DD.
`fact_key` và `fact_value` dùng phát hiện nguồn mâu thuẫn.

```sh
python3 agent.py ingest --file du_lieu_da_duyet.json
```

Lệnh này thay toàn bộ kho bằng tập dữ liệu mới trong một giao dịch.
Khởi động lại ứng dụng sau khi nhập. Bản hiện tại chưa nhập PDF/Excel/Word,
chưa OCR, chưa đồng bộ website hoặc hệ thống bán hàng.

Chính sách bảo hành không đủ để kết luận một serial cụ thể còn được bảo hành.
Giá, tồn kho, đơn hàng và bảo hành cá nhân cần kết nối nguồn nghiệp vụ có
thẩm quyền. “Chuyển nhân viên” hiện chỉ là thông báo, chưa gửi ticket.

## Kiểm tra

```sh
python3 -m unittest discover -s tests -v
```

Xem `VALIDATION.md` để biết kết quả thực tế và phần chưa kiểm chứng.
API local được mô tả trong `API.md`; bộ kiểm tra HTTP ở `postman/`.

## Bước phát triển khi có dữ liệu thật

1. Chuẩn hóa SKU, phiên bản tài liệu, thời hạn và người duyệt.
2. Bổ sung embedding tiếng Việt và truy xuất hybrid; đo bằng bộ câu hỏi thật.
3. Tách thông số/chính sách có cấu trúc khỏi phần hướng dẫn văn xuôi.
4. Kết nối tra cứu đơn hàng/serial với phân quyền và hệ thống tạo ticket.
5. Đo độ đúng nguồn, tỷ lệ từ chối và lỗi nhầm sản phẩm trước khi đưa cho khách.

Ứng dụng lưu hội thoại vào `data/conversations.sqlite3` (theo tài khoản, có thời hạn) và
không tự thao tác đơn hàng. Ollama nhận câu hỏi và các đoạn nguồn qua loopback;
model cloud chỉ được gọi khi quản trị viên bật rõ ràng.

## Đọc hiểu source code

Mở [HUONG_DAN_SOURCE.html](HUONG_DAN_SOURCE.html) để xem hướng dẫn tiếng Việt và ba sơ đồ.
Bản Markdown: [HUONG_DAN_SOURCE.md](HUONG_DAN_SOURCE.md).
Tìm nhãn [A1]–[A8] trong agent.py, [B1]–[B6] trong app.py và [C1]–[C7] trong index.html để đối chiếu với hướng dẫn.


## Bản chat

Có bong bóng hội thoại, Enter gửi/Shift+Enter xuống dòng, Chat mới và nguồn
thu gọn bên dưới câu trả lời. Thử “chính sách bảo hành và đổi trả”; hai chủ đề
được tìm và kiểm tra riêng. Mỗi chủ đề phải có bằng chứng; thông tin chưa có
vẫn bị từ chối. Nội dung chính sách vẫn giữ nguyên văn để không mất ngoại lệ.

Thử tiếp nối: hỏi “đổi trả”, rồi “Chính sách đó có điều kiện gì?”.
Ngữ cảnh chỉ hỗ trợ một số cụm tham chiếu rõ; đây chưa phải hội thoại tự do
như một trợ lý tổng quát. Mỗi lượt có tối đa 4 chủ đề khi bật mô hình.


## Orchestrator, nạp lại ngữ cảnh và trả lời ngoài RAG

### 1. Orchestrator chọn model theo độ nặng công việc (`orchestrator.py`)

| Tác vụ | Chuỗi tầng (thử lần lượt, lỗi thì xuống tầng sau) |
| --- | --- |
| Sửa chính tả / dịch câu hỏi (`normalize_query`) | light → heavy |
| Tóm tắt hội thoại (`summarize_context`) | light → heavy |
| Viết lại câu nối tiếp theo ngữ cảnh (`contextualize_query`) | heavy → light |
| Chọn bằng chứng, câu đơn giản (`select_evidence`) | light → heavy |
| Chọn bằng chứng, câu phức tạp | cloud → heavy → light |
| Trả lời tự do khi RAG không có (`general_answer`) | heavy → light; câu phức tạp: cloud → heavy → light |

```bash
export LOCAL_LIGHT_MODEL='Qwen:latest'     # mặc định = --model
export LOCAL_HEAVY_MODEL='qwen3.8:27b'     # mặc định = model light
# Cloud (tùy chọn, phải bật rõ ràng):
export ALLOW_CLOUD_MODEL=true OPENAI_API_KEY='...' OPENAI_COMPLEX_MODEL='...'
export MODEL_ROUTING_MODE=hybrid           # hybrid | local_only | openai_only
```

Đầu ra mọi tầng đều bị kiểm tra theo JSON schema; ID nguồn lạ, JSON sai, timeout → chuyển tầng.
Mỗi lần thử ghi span `orchestrator.<tác vụ>.<tầng>` trong trace. "Thinking" của Ollama tắt mặc
định cho tác vụ JSON (`OLLAMA_THINK=true` để bật).

### 2. Nạp lại ngữ cảnh cuộc trò chuyện (`conversation_store.py`)

- Mỗi lượt hỏi/đáp được lưu theo tài khoản; người khác không đọc được.
- Tải lại trang sẽ tự mở lại cuộc trò chuyện gần nhất; cột trái liệt kê các cuộc trò chuyện cũ.
- Server tự dựng ngữ cảnh từ dữ liệu đã lưu (không tin lịch sử do trình duyệt gửi):
  tóm tắt các phần cũ + các tin chưa tóm tắt. Cứ 12 tin trôi khỏi cửa sổ 8 tin gần nhất,
  model light tóm tắt lại ở nền.
- Câu nối tiếp (“còn thời hạn thì sao?”) được model heavy viết lại thành câu độc lập trước khi tra cứu.
- `CONVERSATION_RETENTION_DAYS` (mặc định 30) — hội thoại cũ hơn bị xóa khi khởi động.

### 3. Hỏi ngoài RAG → gọi model có system prompt doanh nghiệp (`prompts.py`)

Hồ sơ “làm việc cho ai, làm gì, không được làm gì” ở `data/business_profile.json`
(đổi đường dẫn bằng `BUSINESS_PROFILE_PATH`). System prompt gồm: danh tính trợ lý, công ty,
khách hàng, vai trò, SKU đang hỏi, ngày hiện tại, phạm vi, điều cấm và định dạng JSON.

Khi RAG không có tài liệu (hoặc model chọn bằng chứng báo thiếu), model trả lời với nhãn
“Thông tin tham khảo do AI tạo”, không có citation và không được cache. Các rào chắn:

- Giá, khuyến mãi, tồn kho, đơn hàng, serial/IMEI → chuyển nhân viên, **không gọi model**.
- Model trả `in_scope=false` → câu từ chối cố định trong hồ sơ, không dùng chữ model viết.
- Model trả `needs_human=true` hoặc câu trả lời có số tiền → chuyển nhân viên.
- Nguồn mâu thuẫn và lỗi model vẫn chuyển nhân viên như cũ.
- Tắt tính năng: `GENERAL_ANSWER_ENABLED=false`.
