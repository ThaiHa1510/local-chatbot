"""SQLite response cache with TTL and knowledge-version separation."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time


class ResponseCache:
    def __init__(self, path, ttl=900):
        self.path, self.ttl = Path(path), int(ttl)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT, expires REAL)")

    @staticmethod
    def key(question, product, model_route, knowledge_version):
        data = json.dumps([" ".join(question.lower().split()), product, model_route, knowledge_version],
                          ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()

    def get(self, key):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT value,expires FROM cache WHERE key=?", (key,)).fetchone()
            if not row or row[1] <= time.time():
                if row:
                    db.execute("DELETE FROM cache WHERE key=?", (key,))
                return None
        return json.loads(row[0])

    def put(self, key, value):
        if value.get("status") not in {"evidence", "search_results", "conversation"}:
            return
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                       (key, json.dumps(value, ensure_ascii=False), time.time() + self.ttl))
