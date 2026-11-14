"""Persistent conversations so a customer (or staff) can resume with full context.

Lưu hội thoại theo người dùng trong SQLite. Khi mở lại một cuộc trò chuyện:
  - giao diện hiển thị lại các tin nhắn cũ;
  - server tự dựng lịch sử (không tin lịch sử do trình duyệt gửi);
  - model nhận "tóm tắt cuộc trò chuyện + vài lượt gần nhất" làm ngữ cảnh.
Hội thoại hết hạn sau CONVERSATION_RETENTION_DAYS ngày (mặc định 30).
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3

RECENT_TURNS = 8          # số tin nhắn gần nhất đưa nguyên văn vào ngữ cảnh
SUMMARY_EVERY = 12        # tóm tắt lại khi có thêm từng ấy tin nhắn ngoài cửa sổ gần nhất
MAX_MESSAGES = 400        # chặn hội thoại phình vô hạn


def _now():
    return datetime.now(timezone.utc)


class ConversationStore:
    def __init__(self, path, retention_days=30):
        self.path = Path(path)
        self.retention = timedelta(days=int(retention_days))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY, username TEXT NOT NULL, product TEXT NOT NULL,
                title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', summarized_upto INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, product TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
              CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(username, updated_at);
              CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);
            """)
        self.purge_expired()

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    # ------------------------------------------------------------ lifecycle
    def purge_expired(self):
        cutoff = (_now() - self.retention).isoformat()
        with self.connect() as db:
            db.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,))

    def create(self, username, product, title=""):
        if not isinstance(product, str) or not product:
            raise ValueError("Thiếu mã sản phẩm.")
        conv_id, stamp = secrets.token_hex(12), _now().isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO conversations(id,username,product,title,created_at,updated_at)"
                       " VALUES(?,?,?,?,?,?)", (conv_id, username, product, title[:80] or "Cuộc trò chuyện mới",
                                               stamp, stamp))
        return self.get(conv_id, username)

    def get(self, conv_id, username):
        """Trả hội thoại nếu thuộc về username; ngược lại None (không lộ hội thoại người khác)."""
        if not isinstance(conv_id, str) or not conv_id:
            return None
        with self.connect() as db:
            row = db.execute("SELECT id,product,title,created_at,updated_at,summary,summarized_upto"
                             " FROM conversations WHERE id=? AND username=?", (conv_id, username)).fetchone()
        if not row:
            return None
        return {"id": row[0], "product": row[1], "title": row[2], "created_at": row[3],
                "updated_at": row[4], "summary": row[5], "summarized_upto": row[6]}

    def list(self, username, limit=20):
        with self.connect() as db:
            rows = db.execute("""SELECT c.id,c.product,c.title,c.updated_at,
                    (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id)
                FROM conversations c WHERE c.username=? ORDER BY c.updated_at DESC LIMIT ?""",
                              (username, max(1, min(int(limit), 100)))).fetchall()
        return [{"id": r[0], "product": r[1], "title": r[2], "updated_at": r[3], "messages": r[4]}
                for r in rows]

    def delete(self, conv_id, username):
        with self.connect() as db:
            return db.execute("DELETE FROM conversations WHERE id=? AND username=?",
                              (conv_id, username)).rowcount > 0

    # ------------------------------------------------------------- messages
    def add_turn(self, conv_id, username, product, question, result):
        conv = self.get(conv_id, username)
        if not conv:
            raise PermissionError("Không tìm thấy cuộc trò chuyện.")
        meta = {k: result.get(k) for k in ("reason", "mode", "model_route", "model_tier",
                                           "normalized_question", "context_used", "ai_generated",
                                           "intro", "disclaimer")
                if result.get(k) is not None}
        meta["citations"] = result.get("citations", [])
        stamp = _now().isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO messages(conversation_id,role,content,product,status,meta,created_at)"
                       " VALUES(?,?,?,?,?,?,?)", (conv_id, "user", question, product, "", "{}", stamp))
            db.execute("INSERT INTO messages(conversation_id,role,content,product,status,meta,created_at)"
                       " VALUES(?,?,?,?,?,?,?)", (conv_id, "assistant", result.get("answer", ""), product,
                                                 result.get("status", ""), json.dumps(meta, ensure_ascii=False),
                                                 stamp))
            title = conv["title"]
            if title == "Cuộc trò chuyện mới":
                title = question.strip().replace("\n", " ")[:80]
            db.execute("UPDATE conversations SET updated_at=?, title=?, product=? WHERE id=?",
                       (stamp, title, product, conv_id))
            # Giữ tối đa MAX_MESSAGES tin gần nhất.
            db.execute("""DELETE FROM messages WHERE conversation_id=? AND id NOT IN
                (SELECT id FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?)""",
                       (conv_id, conv_id, MAX_MESSAGES))

    def messages(self, conv_id, username, limit=100):
        if not self.get(conv_id, username):
            return None
        with self.connect() as db:
            rows = db.execute("""SELECT id,role,content,product,status,meta,created_at FROM
                (SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?) ORDER BY id""",
                              (conv_id, max(1, min(int(limit), MAX_MESSAGES)))).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "product": r[3], "status": r[4],
                 **json.loads(r[5] or "{}"), "created_at": r[6]} for r in rows]

    # -------------------------------------------------------------- context
    def history_for_agent(self, conv_id, username, limit=6):
        """Lịch sử câu hỏi theo đúng định dạng agent.conversation_question cần."""
        rows = [m for m in (self.messages(conv_id, username, limit * 2 + 2) or []) if m["role"] == "user"]
        return [{"product": m["product"], "question": m["content"][:2000]} for m in rows[-limit:]]

    def context(self, conv_id, username):
        """Ngữ cảnh để nạp lại cho model: tóm tắt + các tin chưa được tóm tắt (tối đa
        RECENT_TURNS + SUMMARY_EVERY), nên không có đoạn nào của cuộc trò chuyện bị rơi mất."""
        conv = self.get(conv_id, username)
        if not conv:
            return None
        recent = [{"role": m["role"], "content": m["content"]}
                  for m in self.messages(conv_id, username, RECENT_TURNS + SUMMARY_EVERY)
                  if m["id"] > conv["summarized_upto"]]
        return {"summary": conv["summary"], "recent": recent, "product": conv["product"]}

    def pending_summary_turns(self, conv_id, username):
        """Các tin nhắn đã trôi ra khỏi cửa sổ gần nhất nhưng chưa được tóm tắt.

        Trả (turns, last_id) khi đủ SUMMARY_EVERY tin, ngược lại (None, None).
        """
        conv = self.get(conv_id, username)
        if not conv:
            return None, None
        with self.connect() as db:
            rows = db.execute("SELECT id,role,content FROM messages WHERE conversation_id=? ORDER BY id",
                              (conv_id,)).fetchall()
        outside = [r for r in rows[:-RECENT_TURNS] if r[0] > conv["summarized_upto"]] if len(rows) > RECENT_TURNS else []
        if len(outside) < SUMMARY_EVERY:
            return None, None
        return [{"role": r[1], "content": r[2]} for r in outside], outside[-1][0]

    def save_summary(self, conv_id, username, summary, upto_id):
        with self.connect() as db:
            db.execute("UPDATE conversations SET summary=?, summarized_upto=? WHERE id=? AND username=?",
                       (summary[:1500], int(upto_id), conv_id, username))
