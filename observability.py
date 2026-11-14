"""Privacy-aware JSONL tracing and deterministic evaluation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid


class TraceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    @contextmanager
    def span(self, name, actor="system", kind="internal", trace_id=None, metadata=None,
             evaluator=None):
        started = time.perf_counter()
        event = {"trace_id": trace_id or uuid.uuid4().hex, "span_id": uuid.uuid4().hex[:16],
                 "timestamp": datetime.now(timezone.utc).isoformat(), "name": name,
                 "kind": kind, "actor": actor, "metadata": metadata or {}}
        try:
            yield event
            event["status"] = "ok"
        except Exception as exc:
            event["status"] = "error"
            event["error_type"] = type(exc).__name__
            raise
        finally:
            event["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            event["eval"] = evaluator(event) if evaluator else {
                "passed": event["status"] == "ok", "checks": ["call_completed"]}
            with self.lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def recent(self, limit=100):
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 500)):]
        return [json.loads(line) for line in reversed(lines)]


def safe_metadata(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {"input_sha256": hashlib.sha256(raw.encode()).hexdigest(), "input_bytes": len(raw.encode())}
