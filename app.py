"""Loopback API with local authentication, RBAC, cache and tracing."""
import argparse
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from agent import ROOT, all_records, ask, conversation_question, load_records
from cache_store import ResponseCache
from catalog import catalog_files, load_catalog
from conversation_store import ConversationStore
from handoff import HandoffService, should_offer
from observability import TraceStore, safe_metadata
from orchestrator import Orchestrator
from security import AuthStore, require
from support_actions import SupportActions
from urllib.parse import parse_qs, urlsplit


def make_server(port=8765, model="Qwen:latest", auth_db=None, data_dir=None):
    data = Path(data_dir) if data_dir else ROOT / "data"
    busy, records = threading.BoundedSemaphore(1), all_records()
    auth = AuthStore(auth_db or data / "auth.sqlite3")
    bootstrap = auth.bootstrap()
    traces = TraceStore(data / "traces.jsonl")
    cache = ResponseCache(data / "response_cache.sqlite3", os.environ.get("CACHE_TTL_SECONDS", "900"))
    orchestrator = Orchestrator(model, tracer=traces)
    conversations = ConversationStore(data / "conversations.sqlite3",
                                      os.environ.get("CONVERSATION_RETENTION_DAYS", "30"))

    handoffs = HandoffService(conversations, SupportActions(data / "support.sqlite3"))

    def refresh_summary(conv_id, username):
        """Tóm tắt phần hội thoại cũ bằng model nhẹ, chạy nền để không làm chậm câu trả lời."""
        turns, upto = conversations.pending_summary_turns(conv_id, username)
        if not turns:
            return
        def work():
            try:
                previous = (conversations.get(conv_id, username) or {}).get("summary", "")
                call = orchestrator.summarize(previous, turns, timeout=60, actor=username)
                conversations.save_summary(conv_id, username, call.value["summary"], upto)
            except Exception:
                pass  # tóm tắt là tối ưu; lỗi thì giữ nguyên các tin chưa tóm tắt
        threading.Thread(target=work, daemon=True).start()
    catalog = load_catalog()  # bảng giá lỗi định dạng thì dừng ngay khi khởi động
    # Đổi tài liệu hoặc bảng giá đều làm mất hiệu lực cache câu trả lời.
    knowledge_version = "-".join([str((ROOT / "data" / "knowledge.sqlite3").stat().st_mtime_ns)]
                                 + [str(f.stat().st_mtime_ns) for f in catalog_files() if f.exists()])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def respond(self, status, value, content_type="application/json; charset=utf-8", cookie=None):
            data = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
            if cookie: self.send_header("Set-Cookie", cookie)
            self.end_headers(); self.wfile.write(data)
        def trusted(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        def body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 64000: raise ValueError()
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict): raise ValueError()
            return value
        def identity(self):
            cookie = SimpleCookie(self.headers.get("Cookie", "")); item = cookie.get("support_session")
            return auth.session(item.value if item else "")
        def require_identity(self, permission=None, csrf=False):
            identity = self.identity()
            if not identity: raise PermissionError("AUTH_REQUIRED")
            if csrf and self.headers.get("X-CSRF-Token") != identity["csrf"]: raise PermissionError("CSRF_INVALID")
            if permission: require(identity["role"], permission)
            return identity
        def do_GET(self):
            if not self.trusted(): return self.respond(403, {"error": "Host không hợp lệ."})
            if self.path == "/": return self.respond(200, (ROOT / "index.html").read_text(), "text/html; charset=utf-8")
            try:
                me = self.require_identity()
                if self.path == "/api/me": return self.respond(200, me)
                if self.path == "/api/catalog":
                    return self.respond(200, {"products": sorted({r["product"] for r in records if r["product"] != "STORE-POLICY"}), "records": len(records), "catalog_items": len(catalog), "model": model, "routing": orchestrator.describe()})
                if self.path == "/api/conversations":
                    return self.respond(200, {"conversations": conversations.list(me["username"])})
                url = urlsplit(self.path); query = parse_qs(url.query)
                after = int((query.get("after") or ["0"])[0] or 0)
                if url.path.startswith("/api/conversations/"):
                    conv_id = url.path.rsplit("/", 1)[-1]
                    conv = conversations.get(conv_id, me["username"])
                    if not conv: return self.respond(404, {"error": "Không tìm thấy cuộc trò chuyện."})
                    messages = [m for m in conversations.messages(conv_id, me["username"]) if m["id"] > after]
                    return self.respond(200, {"conversation": conv, "messages": messages})
                if url.path == "/api/handoffs":
                    require(me["role"], "ticket.read")
                    return self.respond(200, {"queue": handoffs.queue((query.get("status") or [""])[0])})
                if url.path.startswith("/api/handoffs/"):
                    require(me["role"], "ticket.read")
                    return self.respond(200, handoffs.view(url.path.rsplit("/", 1)[-1], after))
                if self.path.startswith("/api/users"):
                    require(me["role"], "user.manage"); return self.respond(200, {"users": auth.list_users()})
                if self.path.startswith("/api/traces"):
                    require(me["role"], "trace.read"); return self.respond(200, {"traces": traces.recent(100)})
                return self.respond(404, {"error": "Không tìm thấy."})
            except PermissionError as exc: return self.respond(401 if str(exc) == "AUTH_REQUIRED" else 403, {"error": str(exc)})
            except ValueError as exc: return self.respond(400, {"error": str(exc) or "Yêu cầu không hợp lệ."})
        def do_POST(self):
            if not self.trusted(): return self.respond(403, {"error": "Host không hợp lệ."})
            try:
                if self.path == "/api/login":
                    data = self.body(); token, me = auth.login(data.get("username"), data.get("password"))
                    return self.respond(200, me, cookie=f"support_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800")
                if self.path == "/api/logout":
                    self.require_identity(csrf=True); cookie = SimpleCookie(self.headers.get("Cookie", ""))
                    auth.logout(cookie["support_session"].value)
                    return self.respond(200, {"ok": True}, cookie="support_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                me = self.require_identity("chat", csrf=True)
                if self.path == "/api/users":
                    require(me["role"], "user.manage"); data = self.body()
                    return self.respond(201, auth.create_user(data.get("username"), data.get("password"), data.get("role", "viewer")))
                if self.path == "/api/conversations":
                    data = self.body(); product = data.get("product")
                    if not isinstance(product, str) or not product: raise ValueError()
                    return self.respond(201, conversations.create(me["username"], product))
                if self.path.startswith("/api/conversations/") and self.path.endswith("/delete"):
                    conv_id = self.path.split("/")[3]
                    if not conversations.delete(conv_id, me["username"]): return self.respond(404, {"error": "Không tìm thấy cuộc trò chuyện."})
                    return self.respond(200, {"ok": True})
                if self.path == "/api/handoff":
                    data = self.body()
                    try:
                        ticket = handoffs.request(data.get("conversation_id"), me["username"],
                                                  data.get("customer_name", ""), data.get("customer_contact", ""),
                                                  data.get("note", ""), data.get("reason", ""))
                    except ValueError as exc: return self.respond(400, {"error": str(exc)})
                    return self.respond(201, {"ticket_id": ticket["id"], "status": ticket["status"]})
                if self.path == "/api/handoff/cancel":
                    try: return self.respond(200, handoffs.cancel(self.body().get("conversation_id"), me["username"]))
                    except ValueError as exc: return self.respond(400, {"error": str(exc)})
                if self.path.startswith("/api/handoffs/"):
                    _, _, _, ticket_id, action = self.path.split("/")
                    require(me["role"], "ticket.write"); data = self.body()
                    try:
                        if action == "reply": return self.respond(200, handoffs.reply(ticket_id, me["username"], data.get("message")))
                        if action == "resolve": return self.respond(200, handoffs.resolve(ticket_id, me["username"], data.get("note", "")))
                    except ValueError as exc: return self.respond(400, {"error": str(exc)})
                    return self.respond(404, {"error": "Không tìm thấy."})
                if self.path != "/api/ask": return self.respond(404, {"error": "Không tìm thấy."})
                if not busy.acquire(blocking=False): return self.respond(429, {"error": "Agent đang bận."})
                try:
                    data = self.body(); question, product = data.get("question"), data.get("product")
                    if not isinstance(question, str) or not isinstance(product, str): raise ValueError()
                    use_model = data.get("use_model", True)
                    # Nạp lại ngữ cảnh từ server khi có conversation_id; lịch sử do trình duyệt
                    # gửi chỉ dùng cho client cũ chưa có conversation_id.
                    conv_id, context = data.get("conversation_id"), None
                    if conv_id is not None:
                        if not conversations.get(conv_id, me["username"]):
                            return self.respond(404, {"error": "Không tìm thấy cuộc trò chuyện."})
                        history = conversations.history_for_agent(conv_id, me["username"])
                        context = conversations.context(conv_id, me["username"])
                        if not context["summary"] and not context["recent"]: context = None
                    else:
                        history = data.get("history")
                        conv_id = conversations.create(me["username"], product)["id"]
                    conv = conversations.get(conv_id, me["username"])
                    if conv["handoff_status"] in ("waiting", "active"):
                        # Đang gặp nhân viên: bot không trả lời, tin chuyển thẳng cho nhân viên.
                        if len(question) > 2000 or not question.strip(): raise ValueError()
                        result = handoffs.customer_message(conv_id, me["username"], question)
                        result["conversation_id"] = conv_id
                        return self.respond(200, result)
                    previous_statuses = [m["status"] for m in conversations.messages(conv_id, me["username"], 6)
                                         if m["role"] == "assistant"]
                    route = orchestrator.route_label(question) if use_model else "retrieval"
                    key = cache.key(question, product, route, knowledge_version)
                    # Câu tham chiếu ("chính sách đó") phụ thuộc lịch sử nên không đọc cache.
                    follow_up = conversation_question(question, product, history)[1]
                    cached = None if follow_up else cache.get(key)
                    if cached:
                        cached.update({"cache_hit": True, "conversation_id": conv_id,
                                       "handoff_offer": should_offer(cached, previous_statuses)})
                        with traces.span("rag.ask.cache", me["username"], "cache", metadata=safe_metadata(data)) as span: cached["trace_id"] = span["trace_id"]
                        conversations.add_turn(conv_id, me["username"], product, question, cached)
                        return self.respond(200, cached)
                    with traces.span("rag.ask", me["username"], "agent", metadata=safe_metadata(data), evaluator=lambda e: {"passed": e["status"] == "ok", "checks": ["guardrail", "retrieval", "citation_contract"]}) as span:
                        result = ask(question, product, records, model if use_model else "", history=history,
                                     tracer=traces, actor=me["username"], trace_id=span["trace_id"],
                                     orchestrator=orchestrator if use_model else None, context=context,
                                     catalog=catalog)
                        result.update({"cache_hit": False, "trace_id": span["trace_id"], "conversation_id": conv_id,
                                       "handoff_offer": should_offer(result, previous_statuses)})
                    # Chỉ cache câu trả lời không phụ thuộc ngữ cảnh hội thoại.
                    if not result.get("context_used") and not (history and result.get("spelling_corrected")):
                        cache.put(key, {k: v for k, v in result.items() if k != "conversation_id"})
                    conversations.add_turn(conv_id, me["username"], product, question, result)
                    if use_model: refresh_summary(conv_id, me["username"])
                    return self.respond(200, result)
                finally: busy.release()
            except PermissionError as exc: return self.respond(401 if str(exc) == "AUTH_REQUIRED" else 403, {"error": str(exc)})
            except (ValueError, TypeError, json.JSONDecodeError): return self.respond(400, {"error": "Yêu cầu không hợp lệ."})
            except Exception: return self.respond(500, {"error": "Không xử lý được yêu cầu."})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler); server.bootstrap_credentials = bootstrap
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8765); parser.add_argument("--model", default="Qwen:latest")
    # Model lớn cho việc khó (viết SQL tra bảng giá, trả lời tự do). Mặc định dùng chung --model.
    parser.add_argument("--heavy-model", default=os.environ.get("LOCAL_HEAVY_MODEL", "")); args = parser.parse_args()
    if args.heavy_model: os.environ["LOCAL_HEAVY_MODEL"] = args.heavy_model
    if not all_records(): load_records(ROOT / "data" / "mock_knowledge.json")
    server = make_server(args.port, args.model)
    if server.bootstrap_credentials: print("Tài khoản quản trị lần đầu: %s / %s" % server.bootstrap_credentials, flush=True)
    print(f"Mở http://127.0.0.1:{args.port} — Ctrl+C để dừng.", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()
