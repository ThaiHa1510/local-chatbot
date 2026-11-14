"""Ticket storage and guarded staff notifications for the local MCP server."""
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import os
from pathlib import Path
import secrets
import smtplib
import sqlite3

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "support.sqlite3"
PRIORITIES = {"low", "normal", "high", "urgent"}
STATUSES = {"open", "in_progress", "waiting_customer", "resolved", "closed"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SupportActions:
    def __init__(self, db_path=DEFAULT_DB, env=None):
        self.db_path = Path(db_path)
        self.env = os.environ if env is None else env
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, priority TEXT NOT NULL, customer_name TEXT NOT NULL,
                    customer_contact TEXT NOT NULL, product TEXT NOT NULL,
                    subject TEXT NOT NULL, description TEXT NOT NULL, source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, status TEXT NOT NULL, confirmation_token TEXT NOT NULL,
                    subject TEXT NOT NULL, message TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '', FOREIGN KEY(ticket_id) REFERENCES tickets(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
                );
            """)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def clean(value, name, limit, required=True):
        if not isinstance(value, str):
            raise ValueError(f"{name} phải là chuỗi.")
        value = value.strip()
        if required and not value:
            raise ValueError(f"{name} không được để trống.")
        if len(value) > limit:
            raise ValueError(f"{name} vượt quá {limit} ký tự.")
        return value

    def create_ticket(self, subject, description, product="", priority="normal",
                      customer_name="", customer_contact="", source="mcp"):
        if priority not in PRIORITIES:
            raise ValueError("priority phải là low, normal, high hoặc urgent.")
        data = {
            "id": "TKT-" + secrets.token_hex(5).upper(), "created_at": now(),
            "status": "open", "priority": priority,
            "customer_name": self.clean(customer_name, "customer_name", 120, False),
            "customer_contact": self.clean(customer_contact, "customer_contact", 254, False),
            "product": self.clean(product, "product", 80, False),
            "subject": self.clean(subject, "subject", 180),
            "description": self.clean(description, "description", 5000),
            "source": self.clean(source, "source", 40),
        }
        data["updated_at"] = data["created_at"]
        with self.connect() as conn:
            conn.execute("""INSERT INTO tickets
                (id,created_at,updated_at,status,priority,customer_name,customer_contact,
                 product,subject,description,source)
                VALUES (:id,:created_at,:updated_at,:status,:priority,:customer_name,
                        :customer_contact,:product,:subject,:description,:source)""", data)
            conn.execute("INSERT INTO events(ticket_id,created_at,kind,detail) VALUES (?,?,?,?)",
                         (data["id"], now(), "ticket_created", "Created through MCP"))
        return data

    def get_ticket(self, ticket_id):
        ticket_id = self.clean(ticket_id, "ticket_id", 40)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise ValueError("Không tìm thấy ticket.")
        return dict(row)

    def list_tickets(self, status="", limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit phải từ 1 đến 100.")
        if status and status not in STATUSES:
            raise ValueError("Trạng thái ticket không hợp lệ.")
        sql = "SELECT * FROM tickets"
        params = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def update_ticket_status(self, ticket_id, status, note=""):
        if status not in STATUSES:
            raise ValueError("Trạng thái ticket không hợp lệ.")
        ticket = self.get_ticket(ticket_id)
        note = self.clean(note, "note", 1000, False)
        with self.connect() as conn:
            conn.execute("UPDATE tickets SET status=?,updated_at=? WHERE id=?",
                         (status, now(), ticket_id))
            conn.execute("INSERT INTO events(ticket_id,created_at,kind,detail) VALUES (?,?,?,?)",
                         (ticket_id, now(), "status_changed",
                          json.dumps({"from": ticket["status"], "to": status, "note": note},
                                     ensure_ascii=False)))
        return self.get_ticket(ticket_id)

    def notification_config(self):
        mode = self.env.get("SUPPORT_EMAIL_MODE", "console").lower()
        configured = mode == "console" or all(self.env.get(key) for key in
            ("SUPPORT_SMTP_HOST", "SUPPORT_EMAIL_FROM", "SUPPORT_STAFF_EMAIL"))
        return {
            "mode": mode, "configured": configured,
            "staff_email_configured": bool(self.env.get("SUPPORT_STAFF_EMAIL")),
            "smtp_host_configured": bool(self.env.get("SUPPORT_SMTP_HOST")),
            "note": "console chỉ ghi sự kiện local; smtp gửi email thật.",
        }

    def prepare_staff_notification(self, ticket_id, subject="", message=""):
        ticket = self.get_ticket(ticket_id)
        subject = self.clean(subject or f"[{ticket_id}] {ticket['subject']}", "subject", 180)
        message = self.clean(message or ticket["description"], "message", 5000)
        action_id = "ACT-" + secrets.token_hex(6).upper()
        token = secrets.token_urlsafe(18)
        created = datetime.now(timezone.utc)
        expires = created.timestamp() + 900
        expires_at = datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute("""INSERT INTO pending_actions
                (id,ticket_id,created_at,expires_at,status,confirmation_token,subject,message)
                VALUES (?,?,?,?,?,?,?,?)""",
                (action_id, ticket_id, created.isoformat(timespec="seconds"), expires_at,
                 "pending", token, subject, message))
        return {
            "action_id": action_id, "ticket_id": ticket_id, "subject": subject,
            "message": message, "expires_at": expires_at,
            "confirmation_token": token, "confirmation_required": True,
            "destination": self.env.get("SUPPORT_STAFF_EMAIL", "chưa cấu hình"),
            "mode": self.env.get("SUPPORT_EMAIL_MODE", "console").lower(),
        }

    def execute_staff_notification(self, action_id, confirmation_token):
        action_id = self.clean(action_id, "action_id", 40)
        confirmation_token = self.clean(confirmation_token, "confirmation_token", 100)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            raise ValueError("Không tìm thấy hành động chờ xác nhận.")
        action = dict(row)
        if action["status"] != "pending":
            raise ValueError("Hành động này đã được xử lý.")
        if action["confirmation_token"] != confirmation_token:
            raise ValueError("Mã xác nhận không đúng.")
        if datetime.fromisoformat(action["expires_at"]) < datetime.now(timezone.utc):
            raise ValueError("Hành động đã hết hạn; hãy chuẩn bị lại.")
        mode = self.env.get("SUPPORT_EMAIL_MODE", "console").lower()
        if mode == "console":
            result = {"delivery": "simulated", "destination": "local event log"}
        elif mode == "smtp":
            result = self.send_smtp(action["subject"], action["message"])
        else:
            raise ValueError("SUPPORT_EMAIL_MODE phải là console hoặc smtp.")
        with self.connect() as conn:
            conn.execute("UPDATE pending_actions SET status='executed',result=? WHERE id=?",
                         (json.dumps(result, ensure_ascii=False), action_id))
            conn.execute("INSERT INTO events(ticket_id,created_at,kind,detail) VALUES (?,?,?,?)",
                         (action["ticket_id"], now(), "staff_notification", json.dumps(result)))
        return {"action_id": action_id, "ticket_id": action["ticket_id"], **result}

    def send_smtp(self, subject, body):
        config = self.notification_config()
        if not config["configured"]:
            raise ValueError("SMTP chưa được cấu hình đủ.")
        host = self.env["SUPPORT_SMTP_HOST"]
        port = int(self.env.get("SUPPORT_SMTP_PORT", "587"))
        sender = self.env["SUPPORT_EMAIL_FROM"]
        recipient = self.env["SUPPORT_STAFF_EMAIL"]
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = sender, recipient, subject
        message.set_content(body)
        security = self.env.get("SUPPORT_SMTP_SECURITY", "starttls").lower()
        client_cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
        with client_cls(host, port, timeout=20) as client:
            if security == "starttls":
                client.starttls()
            username = self.env.get("SUPPORT_SMTP_USERNAME", "")
            password = self.env.get("SUPPORT_SMTP_PASSWORD", "")
            if username:
                client.login(username, password)
            client.send_message(message)
        return {"delivery": "sent", "destination": recipient, "transport": "smtp"}
