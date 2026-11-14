"""Task orchestrator: pick the cheapest model tier that can do the job, fall back on failure.

Ba tầng mô hình:
  light  – model local nhỏ, nhanh (chuẩn hóa câu hỏi, tóm tắt, chọn bằng chứng đơn giản)
  heavy  – model local lớn (trả lời tự do, chọn bằng chứng phức tạp khi chưa bật cloud)
  cloud  – model cloud (OpenAI-compatible) cho việc phức tạp; CHỈ dùng khi quản trị viên
           bật ALLOW_CLOUD_MODEL=true và có OPENAI_API_KEY.

Mỗi tác vụ được lập kế hoạch thành một chuỗi tầng (ví dụ cloud → heavy → light).
Tầng lỗi (timeout, JSON sai, vi phạm hợp đồng) sẽ chuyển sang tầng kế tiếp.
"""
from dataclasses import dataclass, field
import json
import os
import urllib.request

from model_router import complexity
import prompts

OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"

# Tác vụ → mức tối thiểu cần có. "auto" = tùy độ phức tạp của câu hỏi.
TASKS = {
    "normalize_query": "light",
    "contextualize_query": "heavy",   # viết lại câu nối tiếp cần suy luận hơn sửa chính tả
    "summarize_context": "light",
    "select_evidence": "auto",
    "general_answer": "heavy",
    "write_catalog_sql": "heavy",     # viết SQL chỉ đọc; Python chạy trong hộp cát và tự tra giá
}


@dataclass
class Plan:
    task: str
    complexity: str
    chain: list
    reason: str

    @property
    def primary(self):
        return self.chain[0] if self.chain else "none"


@dataclass
class CallResult:
    value: dict
    tier: str
    model: str
    attempts: list = field(default_factory=list)


class Orchestrator:
    def __init__(self, light_model, env=None, ollama_endpoint=OLLAMA_CHAT, tracer=None, profile=None):
        env = os.environ if env is None else env
        self.env = env
        self.models = {
            "light": env.get("LOCAL_LIGHT_MODEL") or light_model,
            "heavy": env.get("LOCAL_HEAVY_MODEL") or light_model,
            "cloud": env.get("OPENAI_COMPLEX_MODEL", "gpt-6-astra"),
        }
        self.mode = env.get("MODEL_ROUTING_MODE", "hybrid")  # hybrid | local_only | openai_only
        self.cloud_allowed = env.get("ALLOW_CLOUD_MODEL", "false").lower() == "true"
        self.api_key = env.get("OPENAI_API_KEY", "")
        self.base_url = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.general_answer_enabled = env.get("GENERAL_ANSWER_ENABLED", "true").lower() == "true"
        # Tắt "thinking" cho tác vụ JSON: nhanh hơn và không tiêu hết num_predict vào suy nghĩ.
        self.think = env.get("OLLAMA_THINK", "false").lower() == "true"
        self.ollama_endpoint = ollama_endpoint
        self.tracer = tracer
        self._profile = profile

    # ------------------------------------------------------------------ config
    @property
    def cloud_ready(self):
        return self.mode != "local_only" and self.cloud_allowed and bool(self.api_key)

    @property
    def profile(self):
        if self._profile is None:
            self._profile = prompts.load_profile()
        return self._profile

    def describe(self):
        return {"mode": self.mode, "cloud_enabled": self.cloud_ready,
                "general_answer": self.general_answer_enabled,
                "models": {k: v for k, v in self.models.items() if k != "cloud" or self.cloud_ready}}

    # -------------------------------------------------------------- planning
    def plan(self, task, text=""):
        if task not in TASKS:
            raise ValueError("Tác vụ không hỗ trợ: " + task)
        level = complexity(text) if text else "simple"
        need = TASKS[task]
        if need == "light":
            chain, reason = ["light", "heavy"], "tác vụ nhẹ"
        elif need == "auto" and level == "simple":
            chain, reason = ["light", "heavy"], "câu hỏi đơn giản"
        else:
            chain = ["heavy", "light"]
            reason = "câu hỏi phức tạp" if level == "complex" else "cần diễn đạt tự do"
            if self.cloud_ready and (level == "complex" or self.mode == "openai_only"):
                chain.insert(0, "cloud")
        if self.mode == "openai_only" and self.cloud_ready and need != "light" and chain[0] != "cloud":
            chain.insert(0, "cloud")
        # Bỏ tầng trùng model (khi light và heavy cùng một model).
        unique, seen = [], set()
        for tier in chain:
            key = (tier == "cloud", self.models[tier])
            if key not in seen:
                seen.add(key)
                unique.append(tier)
        return Plan(task, level, unique, reason)

    def route_label(self, question):
        """Nhãn tương thích API cũ: local | openai | local_fallback."""
        primary = self.plan("select_evidence", question).primary
        if primary == "cloud":
            return "openai"
        if complexity(question) == "complex" and self.mode != "local_only":
            return "local_fallback"
        return "local"

    # ------------------------------------------------------------- providers
    def _ollama(self, model, messages, schema, timeout, options=None):
        payload = {"model": model, "stream": False, "messages": messages, "think": self.think,
                   "options": {"temperature": 0, "num_ctx": 8192, **(options or {})}}
        if schema:
            payload["format"] = schema
        request = urllib.request.Request(self.ollama_endpoint, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        # Không gửi dữ liệu khách hàng qua proxy cấu hình bằng biến môi trường.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            raw = json.load(response)
        if raw.get("done") is not True:
            raise ValueError("Mô hình chưa hoàn thành câu trả lời.")
        return raw["message"]["content"]

    def _cloud(self, messages, schema, timeout, name="result"):
        instructions = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        conversation = [{"role": m["role"], "content": m["content"]}
                        for m in messages if m["role"] != "system"]
        payload = {"model": self.models["cloud"], "store": False, "instructions": instructions,
                   "input": conversation}
        if schema:
            payload["text"] = {"format": {"type": "json_schema", "name": name,
                                          "strict": True, "schema": schema}}
        request = urllib.request.Request(self.base_url + "/responses", json.dumps(payload).encode(),
                                         {"Content-Type": "application/json",
                                          "Authorization": "Bearer " + self.api_key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.load(response)
        return "".join(item.get("text", "") for output in raw.get("output", [])
                       for item in output.get("content", []) if item.get("type") == "output_text")

    def _call_tier(self, tier, messages, schema, timeout, options, name):
        if tier == "cloud":
            return self._cloud(messages, schema, timeout, name)
        return self._ollama(self.models[tier], messages, schema, timeout, options)

    # ------------------------------------------------------------- execution
    def run_json(self, task, messages, schema, validate, text="", timeout=90, options=None,
                 actor="system", trace_id=None, plan=None):
        """Chạy tác vụ theo chuỗi tầng; trả về CallResult của tầng đầu tiên thành công."""
        plan = plan or self.plan(task, text)
        attempts, last_error = [], None
        for tier in plan.chain:
            model = self.models[tier]
            try:
                if self.tracer:
                    with self.tracer.span(f"orchestrator.{task}.{tier}", actor, "model", trace_id,
                                          {"model": model, "tier": tier, "complexity": plan.complexity}):
                        content = self._call_tier(tier, messages, schema, timeout, options, task)
                        value = validate(json.loads(content))
                else:
                    content = self._call_tier(tier, messages, schema, timeout, options, task)
                    value = validate(json.loads(content))
                attempts.append({"tier": tier, "ok": True})
                return CallResult(value, tier, model, attempts)
            except Exception as exc:  # thử tầng kế tiếp
                attempts.append({"tier": tier, "ok": False, "error": type(exc).__name__})
                last_error = exc
        raise RuntimeError(f"Mọi tầng mô hình đều lỗi cho tác vụ {task}") from last_error

    # ------------------------------------------------------------ task APIs
    def normalize(self, question, previous_questions=None, **kw):
        task = "contextualize_query" if previous_questions else "normalize_query"
        return self.run_json(task, prompts.normalize_messages(question, previous_questions),
                             prompts.NORMALIZE_SCHEMA, prompts.validate_normalized,
                             options={"num_predict": 180, "num_ctx": 4096}, **kw)

    def select_evidence(self, question, candidates, **kw):
        return self.run_json("select_evidence", prompts.evidence_messages(question, candidates),
                             prompts.evidence_schema(candidates),
                             lambda r: prompts.validate_selection(r, candidates),
                             text=question, options={"num_predict": 250}, **kw)

    def general_answer(self, question, product, catalog=(), context=None, **kw):
        messages = prompts.general_answer_messages(self.profile, question, product, catalog, context)
        return self.run_json("general_answer", messages, prompts.GENERAL_ANSWER_SCHEMA,
                             prompts.validate_general_answer, text=question,
                             options={"num_predict": 600}, **kw)

    def write_catalog_sql(self, question, selected_product, schema_doc, check=None, **kw):
        """Model viết một câu SELECT trên bảng products; không nhận giá, không viết câu trả lời.

        check: hàm chạy thử SQL trong hộp cát; SQL hỏng thì chuyển sang tầng model kế tiếp.
        """
        def validate(result):
            result = prompts.validate_catalog_sql(result)
            return check(result) if check else result
        return self.run_json("write_catalog_sql",
                             prompts.catalog_sql_messages(question, selected_product, schema_doc),
                             prompts.CATALOG_SQL_SCHEMA, validate,
                             text=question, options={"num_predict": 500, "num_ctx": 8192}, **kw)

    def summarize(self, previous_summary, turns, **kw):
        return self.run_json("summarize_context", prompts.summary_messages(previous_summary, turns),
                             prompts.SUMMARY_SCHEMA, prompts.validate_summary,
                             options={"num_predict": 400}, **kw)
