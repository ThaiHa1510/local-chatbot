"""Local evidence-only RAG. Python 3.9+, no third-party dependencies."""
import argparse
from collections import Counter
from datetime import date
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
import urllib.request
import catalog as catalog_store
import catalog_sql
from guardrails import inspect_input
import handoff
import injection
from model_router import ModelRouter
import prompts

# [A1] Cấu hình đường dẫn: kho dữ liệu nằm cạnh mã nguồn; Ollama chạy trên máy.
ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "knowledge.sqlite3"
OLLAMA = "http://127.0.0.1:11434/api/chat"
STORE_SCOPE = "STORE-POLICY"
STORE_INTENT = {
    "giao", "ship", "van", "chuyen", "dia", "chi", "hotline", "email",
    "thanh", "toan", "cod", "vat", "hoa", "don", "huy", "otp", "mo", "dong",
    "danh", "sach", "nhanh", "gan", "he", "thong",
}
STOP = set("cho toi minh ban cua la co khong bao nhieu the nao duoc voi va thi mot san pham xin hoi ve cac".split())


def tokens(text):
    # [A2] Đưa về chữ thường, bỏ dấu, tách từ và bỏ từ phổ biến để tìm kiếm.
    # Đây không phải embedding; bỏ từ phủ định có thể làm mất sắc thái câu hỏi.
    text = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return [t for t in re.findall(r"[a-z0-9]+", text) if t not in STOP]


def load_records(path, db=DB):
    # [A3] Kiểm tra toàn bộ JSON trước khi thay kho. Mỗi bản ghi là một đoạn
    # đã chia sẵn; không tự đọc PDF hay tự chia tài liệu dài.
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("Dữ liệu phải là danh sách bản ghi không rỗng.")
    ids = set()
    required = ("id", "product", "topic", "text", "source", "valid_from", "valid_to")
    for r in records:
        if any(not isinstance(r.get(k), str) or not r[k].strip() for k in required):
            raise ValueError("Thiếu trường bắt buộc hoặc sai kiểu dữ liệu.")
        if r["id"] in ids or len(r["text"]) > 5000:
            raise ValueError("ID trùng hoặc đoạn tài liệu quá dài.")
        ids.add(r["id"])
        if date.fromisoformat(r["valid_from"]) > date.fromisoformat(r["valid_to"]):
            raise ValueError("Thời hạn không hợp lệ.")
        if type(r.get("approved")) is not bool or type(r.get("mock")) is not bool:
            raise ValueError("approved và mock phải là boolean.")
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    # Giao dịch SQLite: thay toàn bộ dữ liệu, không thêm nối vào kho cũ.
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        conn.execute("DELETE FROM records")
        conn.executemany("INSERT INTO records VALUES (?,?)",
                         [(r["id"], json.dumps(r, ensure_ascii=False)) for r in records])
    return len(records)


def all_records(db=DB):
    # [A4] Nạp các bản ghi JSON từ SQLite vào bộ nhớ.
    if not Path(db).exists():
        return []
    with sqlite3.connect(db) as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT body FROM records ORDER BY id")]


def retrieval_queries(question, product):
    """[A5a] Tách các chủ đề nối bằng 'và'; bỏ cụm dẫn không mang nội dung."""
    folded = unicodedata.normalize("NFD", question.lower().replace("đ", "d"))
    folded = "".join(c for c in folded if unicodedata.category(c) != "Mn")
    for phrase in ("chinh sach", "cho minh biet", "cho toi biet", "tu van", "thi sao"):
        folded = folded.replace(phrase, " ")
    clauses = re.split(r"\b(?:va|cung nhu)\b|[;?]", folded)
    return [q for part in clauses if (q := set(tokens(part)) - set(tokens(product)))]


def matches_query(query, doc):
    # Hai từ ngắn như “đổi trả” phải khớp cả hai, không nhầm “kiểm tra”.
    threshold = 1.0 if len(query) <= 2 else (0.75 if len(query) <= 4 else 0.45)
    return len(query & doc) / len(query) >= threshold


def is_store_query(query):
    """Nhận diện ý hỏi thuộc vận hành cửa hàng để ưu tiên đúng phạm vi nguồn."""
    return bool(hinted_fact_keys(query))


def hinted_fact_keys(query):
    """Ánh xạ các ý cửa hàng rõ ràng sang nhóm dữ liệu có cấu trúc."""
    location_phrases = (
        {"dia", "chi"} <= query,
        {"danh", "sach"} <= query and "hang" in query,
        {"chi", "nhanh"} <= query,
        "hang" in query and bool({"gan", "nhanh"} & query),
        {"he", "thong"} <= query and "hang" in query,
    )
    if any(location_phrases):
        return {"store_address"}
    if "gio" in query and ({"mo", "dong"} & query):
        return {"opening_hours"}
    if {"thanh", "toan"} <= query:
        return {"payment_methods"}
    if "hotline" in query or "email" in query or "lien" in query:
        return {"contact"}
    if "vat" in query or ({"hoa", "don"} <= query):
        return {"vat_invoice"}
    if "otp" in query or "mat" in query or "the" in query:
        return {"privacy"}
    if "giao" in query or "ship" in query or ({"van", "chuyen"} <= query):
        return {"shipping_fee"} if "phi" in query else {"shipping_scope"}
    return set()


def covers_questions(question, product, rows):
    """Mỗi ý phải có ít nhất một đoạn đạt ngưỡng; không bỏ qua ý chưa có nguồn."""
    queries = retrieval_queries(question, product)
    docs = [set(tokens(r["topic"] + " " + r.get("keywords", "") + " " + r["text"]))
            for r in rows]
    return bool(queries) and all(any(
        matches_query(q, doc) and
        (not hinted_fact_keys(q) or row.get("fact_key") in hinted_fact_keys(q))
        for row, doc in zip(rows, docs)) for q in queries)


def conversation_question(question, product, history):
    """[A7a] Ngữ cảnh giới hạn: chỉ câu hỏi trước cùng SKU, không tin câu trả lời cũ."""
    if history is None:
        history = []
    if not isinstance(history, list) or len(history) > 6:
        raise ValueError("Lịch sử tối đa 6 lượt hỏi.")
    for turn in history:
        if (not isinstance(turn, dict) or not isinstance(turn.get("product"), str)
                or not isinstance(turn.get("question"), str)
                or not 1 <= len(turn["question"]) <= 2000):
            raise ValueError("Lịch sử không hợp lệ.")
    # Chỉ nối khi có tham chiếu rõ; câu hỏi mới có nội dung riêng dùng chính nó.
    folded = unicodedata.normalize("NFD", question.lower().replace("đ", "d"))
    folded = "".join(c for c in folded if unicodedata.category(c) != "Mn")
    references = ("chinh sach do", "dieu do", "cai do", "truong hop do")
    if any(phrase in folded for phrase in references):
        previous = next((t["question"] for t in reversed(history) if t["product"] == product), None)
        if previous:
            references_pattern = r"chính sách đó|điều đó|cái đó|trường hợp đó|chinh sach do|dieu do|cai do|truong hop do"
            return re.sub(references_pattern, previous.strip(" .!?"), question, flags=re.I), True
    return question, False


FOLLOW_UP_STARTS = ("con ", "the con", "vay ", "vay thi", "the thi", "neu vay", "con neu", "con cai",
                    "and ", "what about", "how about")
FOLLOW_UP_PARTS = ("thi sao", "the nao nua", "cai kia", "cai nay", "no co", "con khong", "nhu vay")


def looks_like_follow_up(question):
    """Heuristic: câu ngắn hoặc mở đầu kiểu 'còn…', 'thế còn…', '…thì sao' thường dựa vào câu trước."""
    folded = unicodedata.normalize("NFD", question.lower().replace("đ", "d"))
    folded = " ".join("".join(c for c in folded if unicodedata.category(c) != "Mn").split())
    return (folded.startswith(FOLLOW_UP_STARTS) or any(p in folded for p in FOLLOW_UP_PARTS)
            or len(tokens(folded)) <= 2)


def retrieve(question, product, records, today=None):
    """BM25 trên tài liệu của SKU và chính sách chung toàn cửa hàng."""
    # [A5] Lọc đúng SKU, đã duyệt và còn hiệu lực trước khi chấm điểm.
    # Khi không truyền today, dùng ngày hệ điều hành của máy chạy ứng dụng.
    today = today or date.today().isoformat()
    scopes = {product, STORE_SCOPE}
    rows = [r for r in records if r["product"] in scopes and r["approved"]
            and r["valid_from"] <= today <= r["valid_to"]]
    queries = retrieval_queries(question, product)
    query = set().union(*queries) if queries else set()
    if not rows or not query:
        return []
    # Tìm trên chủ đề + từ khóa hỗ trợ + nguyên văn nội dung.
    corpus = [tokens(r["topic"] + " " + r.get("keywords", "") + " " + r["text"]) for r in rows]
    # BM25: ưu tiên từ ít phổ biến, giảm lợi thế của đoạn quá dài.
    # Tham số: k1=1.2, b=0.75; ngưỡng phủ từ 0.45; lấy tối đa 5 đoạn.
    avg = sum(map(len, corpus)) / len(corpus)
    df = Counter(t for doc in corpus for t in set(doc))
    scored = []
    for row, doc in zip(rows, corpus):
        counts = Counter(doc)
        overlap = query & counts.keys()
        scoped_queries = [q for q in queries
                          if (row["product"] == STORE_SCOPE) == is_store_query(q)
                          and (not hinted_fact_keys(q)
                               or row.get("fact_key") in hinted_fact_keys(q))]
        # This is a retrieval heuristic, not a calibrated confidence probability.
        if not any(matches_query(q, counts.keys()) for q in scoped_queries):
            continue
        score = sum(math.log(1 + (len(rows) - df[t] + .5) / (df[t] + .5))
                    * counts[t] * 2.2 / (counts[t] + 1.2 * (.25 + .75 * len(doc) / avg))
                    for t in overlap)
        scored.append((score, row))
    # Injection gián tiếp: đoạn tài liệu chứa chỉ thị nhắm vào model không được đưa vào ngữ cảnh.
    ranked = [r for _, r in sorted(scored, key=lambda item: item[0], reverse=True)]
    return [r for r in ranked if not injection.scan_document(r["text"])][:5]


def select_evidence(question, candidates, model, endpoint=OLLAMA, timeout=90):
    """The model selects IDs; it cannot write facts into the final answer."""
    # [A6] Mô hình chỉ chọn ID trong candidates; không sinh nội dung trả lời.
    # enum giới hạn nguồn hợp lệ ngay trong định dạng đầu ra Ollama.
    # Prompt là một lớp hướng dẫn, không phải bảo đảm chống prompt injection (xem prompts.py).
    payload = {
        "model": model, "stream": False, "format": prompts.evidence_schema(candidates),
        "options": {"temperature": 0, "num_predict": 250, "num_ctx": 8192},
        "messages": prompts.evidence_messages(question, candidates),
    }
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    # Never send customer data through an environment-configured external proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = json.load(response)
    # Kiểm tra lại ở ứng dụng, kể cả khi đã có schema ràng buộc phía mô hình.
    if raw.get("done") is not True:
        raise ValueError("Mô hình chưa hoàn thành câu trả lời.")
    return prompts.validate_selection(json.loads(raw["message"]["content"]), candidates)


def normalize_question_llm(question, model, endpoint=OLLAMA, timeout=30):
    """Use the local LLM to correct typos and translate the search query to Vietnamese.

    This output is only a retrieval query. It can never become an answer or a fact.
    """
    payload = {
        "model": model, "stream": False, "format": prompts.NORMALIZE_SCHEMA,
        "options": {"temperature": 0, "num_predict": 180, "num_ctx": 4096},
        "messages": prompts.normalize_messages(question),
    }
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = json.load(response)
    if raw.get("done") is not True:
        raise ValueError("Mô hình chưa hoàn thành chuẩn hóa.")
    result = prompts.validate_normalized(json.loads(raw["message"]["content"]))
    if not inspect_input(result["normalized"])["allowed"]:
        raise ValueError("Kết quả chuẩn hóa không an toàn.")
    return result


def catalog_answer(question, product, catalog, today=None, model="", orchestrator=None, timeout=90,
                   span_kw=None):
    """Tra bảng giá. Thứ tự: chặn câu nhạy cảm → tên máy cụ thể (luật) → model viết SQL → luật.

    Model chỉ viết SQL chạy trong hộp cát chỉ đọc (catalog_sql); câu trả lời do Python ghép.
    """
    meta = {"mode": "catalog", "context_used": False, "normalized_question": None, "spelling_corrected": False}
    if catalog_sql.is_sensitive(question):
        return {**meta, "status": "handoff", "reason": "sensitive_request", "citations": [], "mock": False,
                "catalog_parser": "guard",
                "answer": "Mình không thể cung cấp thông tin nội bộ hoặc thông tin cá nhân. "
                          "Bạn có thể hỏi về giá bán, thông số và so sánh sản phẩm."}
    spec = catalog_store.parse(question, product, catalog, today)
    # Tên máy cụ thể ("giá iphone 17 pro") khớp alias chính xác hơn model nên giữ ở luật.
    if not (spec and spec["kind"] in ("lookup", "compare", "specs")) and model and orchestrator \
            and catalog_sql.looks_catalog(question, catalog):
        conn = catalog_sql.build_db(catalog, today)

        def check(value):  # chạy thử ngay trong bước gọi model để SQL hỏng được thử lại ở tầng khác
            if value["is_catalog_query"] and not value["unsupported_reason"].strip():
                value["intent"] = catalog_sql.validate_intent(value["intent"], question)
                value["rows"] = catalog_sql.run_query(conn, value["sql"])
            return value
        try:
            call = orchestrator.write_catalog_sql(question, product, catalog_sql.schema_doc(catalog, today),
                                                  check=check, timeout=max(timeout, 120), **(span_kw or {}))
            value = call.value
            route = {"catalog_parser": "sql", "model_tier": call.tier,
                     "model_route": "openai" if call.tier == "cloud" else "local"}
            if value["is_catalog_query"] and value["unsupported_reason"].strip():
                return {**meta, **route, "status": "handoff", "reason": "catalog_unsupported", "citations": [],
                        "mock": False, "answer": "Bảng giá hiện chưa có dữ liệu để trả lời chính xác câu này. "
                                                 "Mình có thể chuyển nhân viên tư vấn thêm cho bạn."}
            if value["is_catalog_query"]:
                columns, rows = value["rows"]
                result = catalog_sql.answer_from_rows(value["intent"], value["sql"], columns, rows, catalog)
                return {**meta, **result, **route}
            if spec is None:
                return None  # model xác nhận không phải câu hỏi bảng giá
        except Exception:
            pass  # SQL sai/bị từ chối hoặc model lỗi: dùng luật bên dưới
    if spec is not None and spec["kind"] != "specs" and catalog_store.beyond_rules(question):
        # Luật không hiểu "hiệu năng", "pin trâu"…: nói thật thay vì trả danh sách theo giá dễ gây hiểu nhầm.
        return {**meta, "status": "handoff", "reason": "catalog_unsupported", "citations": [], "mock": False,
                "catalog_parser": "rules",
                "answer": "Bảng giá hiện chưa có dữ liệu để trả lời chính xác câu này. "
                          "Mình có thể chuyển nhân viên tư vấn thêm cho bạn."}
    if spec is None:
        return None
    result = catalog_store.answer(spec, catalog, today)
    result.update({**meta, "catalog_query": spec, "catalog_parser": "rules",
                   "model_route": None, "model_tier": None})
    return result


def ask(question, product, records, model="", today=None, endpoint=OLLAMA, timeout=90, history=None,
        router=None, tracer=None, actor="anonymous", trace_id=None, orchestrator=None, context=None,
        catalog=None):
    # [A7] Bộ điều phối: kiểm tra đầu vào → truy xuất → kiểm tra mâu thuẫn
    # → chọn nguồn bằng mô hình (nếu bật) → trả bằng chứng, hoặc (khi có orchestrator)
    # để model trả lời tự do có system prompt doanh nghiệp, hoặc từ chối.
    # orchestrator: nếu có, mọi lời gọi model đi qua orchestrator (light/heavy/cloud).
    # context: ngữ cảnh hội thoại đã lưu {"summary", "recent"} — chỉ là dữ liệu tham khảo.
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError("Câu hỏi phải có từ 1 đến 2.000 ký tự.")
    if not isinstance(product, str):
        raise ValueError("Mã sản phẩm phải là chuỗi.")
    # Khách đòi gặp người: trả lời cố định, không gọi model → không cần chờ NeMo (luật vẫn phải sạch).
    if handoff.wants_human(question) and injection.scan(question)["verdict"] == "allow":
        return {"status": "handoff", "reason": "human_requested", "handoff_offer": True,
                "answer": "Dạ, mình sẽ kết nối bạn với nhân viên CSKH. Bạn vui lòng để lại tên và "
                          "số điện thoại hoặc email bên dưới để nhân viên hỗ trợ nhé.",
                "citations": [], "mock": False, "mode": "handoff", "context_used": False}

    guard = inspect_input(question)
    if not guard["allowed"]:
        return {"status": "blocked", "reason": guard["reason"],
                "answer": "Mình không thể làm theo yêu cầu thay đổi quy tắc hoặc tiết lộ chỉ thị nội bộ. Bạn hãy hỏi lại về sản phẩm, giao hàng, bảo hành hoặc cửa hàng.",
                "citations": [], "mock": False, "mode": "guardrail", "context_used": False}

    resolved_question, context_used = conversation_question(question, product, history)
    span_kw = {"actor": actor, "trace_id": trace_id}

    # [A7c] Câu hỏi danh mục ("laptop nào rẻ nhất", "giá A14") trả lời bằng truy vấn
    # có cấu trúc trên bảng giá đã duyệt, không qua RAG văn bản và không cần chọn SKU.
    if catalog:
        result = catalog_answer(question, product, catalog, today, model, orchestrator, timeout, span_kw)
        if result:
            return result

    def abstain(reason, message):
        # handoff chỉ là trạng thái UI; chưa tạo ticket hay gửi cho nhân viên.
        # mock=True ở nhánh này hiện được cố định cho bản demo.
        return {"status": "handoff", "reason": reason, "answer": message,
                "citations": [], "mock": any(r.get("mock", False) for r in records),
                "context_used": context_used, "mode": "local_model" if model else "retrieval"}

    selectable_products = {r["product"] for r in records if r["product"] != STORE_SCOPE}

    def fallback(reason, message):
        """[A7b] RAG không có tài liệu → gọi model với system prompt doanh nghiệp.

        Không bao giờ dùng cho giá/tồn kho/đơn hàng/serial, xung đột nguồn hay lỗi model.
        Câu trả lời được gắn nhãn AI, không có citation và không được cache.
        """
        if not (model and orchestrator and orchestrator.general_answer_enabled):
            return abstain(reason, message)
        profile = orchestrator.profile
        if (prompts.requires_authoritative_source(question)
                or prompts.requires_authoritative_source(normalized_question)):
            return abstain("authoritative_source_required", profile["handoff_message"])
        try:
            call = orchestrator.general_answer(normalized_question, product, sorted(selectable_products),
                                               context, timeout=timeout, **span_kw)
        except Exception:
            return abstain(reason, message)
        value = call.value
        result = {"citations": [], "mock": False, "context_used": context_used or bool(context),
                  "mode": "openai_model" if call.tier == "cloud" else "general_model",
                  "model_route": "openai" if call.tier == "cloud" else "local",
                  "model_tier": call.tier, "ai_generated": True,
                  "normalized_question": normalized_question if spelling_corrected else None,
                  "spelling_corrected": spelling_corrected}
        if not value["in_scope"]:
            result.update(status="out_of_scope", reason="out_of_scope",
                          answer=profile["out_of_scope_message"], ai_generated=False)
        elif injection.leaks_prompt(value["answer"]):
            # Model bị dẫn dắt chép lại chỉ thị nội bộ: không trả nội dung này cho khách.
            result.update(status="blocked", reason="prompt_leak", ai_generated=False,
                          answer="Mình không thể chia sẻ thông tin cấu hình nội bộ. "
                                 "Bạn cần hỗ trợ gì về sản phẩm hoặc dịch vụ của cửa hàng ạ?")
        elif value["needs_human"] or prompts.contains_money(value["answer"]):
            # Model tự nhận cần nhân viên, hoặc lỡ nêu số tiền: không đưa nội dung đó cho khách.
            answer = profile["handoff_message"] if prompts.contains_money(value["answer"]) else value["answer"]
            result.update(status="handoff", reason="needs_human", answer=answer)
        else:
            result.update(status="model_answer", reason="no_evidence_model_fallback",
                          answer=value["answer"],
                          disclaimer="Thông tin tham khảo do AI tạo, không dựa trên tài liệu đã duyệt của cửa hàng.")
        return result

    if product not in selectable_products:
        return abstain("product_required", "Vui lòng chọn đúng mã sản phẩm trước khi hỏi.")
    if question.strip().lower().strip(".!? ") in {"xin chào", "chào", "hello", "hi", "cảm ơn", "thanks"}:
        return {"status": "conversation", "reason": "greeting", "answer":
                "Chào bạn! Mình có thể hỗ trợ sản phẩm " + product +
                " cùng thông tin giao hàng, cửa hàng, thanh toán và đặt hàng. Bạn muốn tìm hiểu phần nào?",
                "citations": [], "mock": any(r.get("mock", False) for r in records),
                "mode": "local_model" if model else "retrieval", "context_used": False}
    normalized_question = resolved_question
    spelling_corrected = False
    candidates = retrieve(normalized_question, product, records, today)
    skip_normalize = orchestrator is not None and prompts.requires_authoritative_source(resolved_question)
    previous = [t["question"] for t in (history or []) if t["product"] == product]
    # Câu nối tiếp ("còn thời hạn thì sao?") có thể khớp nhầm tài liệu bằng từ khóa,
    # nên phải viết lại theo ngữ cảnh TRƯỚC khi tin vào kết quả truy xuất.
    pending_follow_up = bool(orchestrator and previous and not context_used
                             and looks_like_follow_up(resolved_question))
    if model and not skip_normalize and (pending_follow_up or not candidates
                                         or not covers_questions(normalized_question, product, candidates)):
        # Chỉ gọi bước sửa lỗi khi truy xuất nguyên bản chưa đủ, tránh tăng độ trễ FAQ chuẩn.
        try:
            if orchestrator:
                # light để sửa chính tả; heavy khi cần viết lại câu nối tiếp bằng câu hỏi trước.
                normalized = orchestrator.normalize(resolved_question, previous,
                                                    timeout=min(timeout, 30), **span_kw).value
                if not inspect_input(normalized["normalized"])["allowed"]:
                    raise ValueError("Kết quả chuẩn hóa không an toàn.")
            elif tracer:
                with tracer.span("model.ollama.normalize_query", actor, "model", trace_id,
                                 {"model": model}):
                    normalized = normalize_question_llm(resolved_question, model, endpoint, min(timeout, 30))
            else:
                normalized = normalize_question_llm(resolved_question, model, endpoint, min(timeout, 30))
            normalized_question = normalized["normalized"]
            spelling_corrected = normalized["changed"] and normalized_question != resolved_question
            if orchestrator and spelling_corrected and previous:
                context_used = True  # câu nối tiếp đã được viết lại nhờ câu hỏi trước
            candidates = retrieve(normalized_question, product, records, today)
        except Exception:
            # Chuẩn hóa là tăng cường truy xuất; lỗi không được làm hỏng câu hỏi gốc.
            normalized_question = resolved_question
    if not candidates or not covers_questions(normalized_question, product, candidates):
        return fallback("insufficient_evidence",
                        "Mình chưa có đủ tài liệu để trả lời toàn bộ câu hỏi này. Bạn có thể hỏi rõ từng ý; phần chưa có thông tin cần nhân viên kiểm tra.")
    active = [r for r in records if r["product"] in {product, STORE_SCOPE} and r["approved"]
              and r["valid_from"] <= (today or date.today().isoformat()) <= r["valid_to"]]
    # Structured fact keys let deterministic checks catch contradictory policy revisions.
    # So sánh fact_value theo fact_key trong các nguồn đang hiệu lực của SKU.
    # Chỉ xét khóa xuất hiện trong candidates; không phát hiện mọi mâu thuẫn văn xuôi.
    for candidate in candidates:
        key = candidate.get("fact_key")
        values = {r.get("fact_value") for r in active if key and r.get("fact_key") == key}
        if len(values) > 1:
            return abstain("conflicting_sources",
                           "Có nguồn còn hiệu lực mâu thuẫn về cùng thông tin. Cần nhân viên xác minh.")
    selected_route = "retrieval"
    used_tiers = []
    if model:
        if orchestrator:
            selected_route = orchestrator.route_label(normalized_question)
        else:
            router = router or ModelRouter()
            selected_route = router.route(normalized_question)
        try:
            # Mỗi chủ đề được kiểm tra riêng để mô hình nhỏ không nhầm thời hạn
            # bảo hành với thời hạn đổi trả. Chỉ trả khi mọi chủ đề có nguồn.
            parts = [p.strip() for p in re.split(r"\b(?:và|cũng như|va|cung nhu)\b|[;?]",
                                                normalized_question, flags=re.I) if p.strip()]
            if len(parts) > 4:
                return abstain("too_many_topics", "Bạn giúp mình chia câu hỏi thành tối đa 4 ý mỗi lượt nhé.")
            results = []
            for part in parts:
                evidence = retrieve(part, product, candidates, today)
                if not evidence:
                    return abstain("insufficient_evidence", "Mình chưa tìm đủ bằng chứng cho tất cả các ý bạn hỏi.")
                part_queries = retrieval_queries(part, product)
                structured_hint = set().union(*(hinted_fact_keys(q) for q in part_queries))
                if structured_hint:
                    # Ý cửa hàng rõ ràng đã được định tuyến bằng fact_key; dùng trực tiếp
                    # các nguồn hợp lệ để tránh mô hình nhỏ chọn nhầm loại chính sách.
                    results.append({"ids": [r["id"] for r in evidence
                                            if r.get("fact_key") in structured_hint],
                                    "insufficient": False, "conflict": False})
                elif orchestrator:
                    # Độ phức tạp tính trên cả câu hỏi, để câu phức tạp vẫn được đưa lên tầng mạnh.
                    plan = orchestrator.plan("select_evidence", normalized_question)
                    call = orchestrator.select_evidence(part, evidence, timeout=timeout, plan=plan, **span_kw)
                    used_tiers.append(call.tier)
                    results.append(call.value)
                elif selected_route == "openai":
                    if tracer:
                        with tracer.span("model.openai.responses", actor, "model", trace_id,
                                         {"model": router.openai_model, "candidate_count": len(evidence)}):
                            results.append(router.openai_select(part, evidence, timeout))
                    else:
                        results.append(router.openai_select(part, evidence, timeout))
                elif tracer:
                    with tracer.span("model.ollama.chat", actor, "model", trace_id,
                                     {"model": model, "candidate_count": len(evidence)}):
                        results.append(select_evidence(part, evidence, model, endpoint, timeout))
                else:
                    results.append(select_evidence(part, evidence, model, endpoint, timeout))
            result = {"ids": list(dict.fromkeys(i for r in results for i in r["ids"])),
                      "insufficient": any(r["insufficient"] or not r["ids"] for r in results),
                      "conflict": any(r["conflict"] for r in results)}
        except Exception:
            # Lỗi mạng, timeout hoặc JSON sai: dừng trả lời, không tự đoán tiếp.
            return abstain("model_unavailable",
                           "Không kiểm chứng được kết quả mô hình local. Vui lòng thử lại hoặc chuyển nhân viên.")
        if used_tiers:
            selected_route = "openai" if "cloud" in used_tiers else "local"
        if result["conflict"]:
            return abstain("conflicting_sources", "Các nguồn có thể mâu thuẫn. Cần nhân viên xác minh.")
        if result["insufficient"] or not result["ids"]:
            return fallback("insufficient_evidence", "Tài liệu chưa đủ để trả lời câu hỏi. Cần nhân viên kiểm tra.")
        candidates = [r for r in candidates if r["id"] in result["ids"]]
        if not covers_questions(normalized_question, product, candidates):
            return abstain("insufficient_evidence", "Mình chưa tìm đủ bằng chứng cho tất cả các ý bạn hỏi. Bạn có thể tách câu hỏi để mình hỗ trợ từng phần.")
    else:
        # Without a model this is a document search, never represented as a verified answer.
        candidates = candidates[:3]
    # Nội dung citations lấy trực tiếp từ kho, không lấy văn bản mô hình tự viết.
    return {
        "status": "evidence" if model else "search_results",
        "reason": "sources_selected" if model else "retrieval_only",
        "answer": "Mình tìm được thông tin sau:" if model else
                  "Kết quả tra cứu tham khảo; chưa xác minh đủ để trả lời:",
        "citations": candidates, "mock": any(r["mock"] for r in candidates),
        "mode": "openai_model" if selected_route == "openai" else ("local_model" if model else "retrieval"),
        "model_route": selected_route,
        "model_tier": used_tiers[-1] if used_tiers else None,
        "normalized_question": normalized_question if spelling_corrected else None,
        "spelling_corrected": spelling_corrected,
        "context_used": context_used,
    }


 # [A8] Chạy độc lập từ Terminal: ingest để nhập kho, ask để thử câu hỏi.
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["ingest", "ask"])
    parser.add_argument("--file", default=str(ROOT / "data" / "mock_knowledge.json"))
    parser.add_argument("--question", default="")
    parser.add_argument("--product", default="")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    if args.command == "ingest":
        print("Đã nạp", load_records(args.file), "đoạn tài liệu.")
    else:
        print(json.dumps(ask(args.question, args.product, all_records(), args.model),
                         ensure_ascii=False, indent=2))
