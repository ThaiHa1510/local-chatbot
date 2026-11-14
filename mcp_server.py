"""Dependency-free MCP stdio server for local customer-support operations."""
import json
import os
import sys
import traceback
from guardrails import guard_tool_arguments
from observability import TraceStore, safe_metadata
from pathlib import Path
from security import require
from support_actions import SupportActions

SERVER_INFO = {"name": "local-support-operations", "version": "1.0.0"}

TOOLS = [
    {
        "name": "support_config_status",
        "description": "Check whether staff email delivery is simulated or configured for real SMTP.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "create_support_ticket",
        "description": "Create a local customer-support ticket. This does not contact staff by itself.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "subject": {"type": "string", "minLength": 1, "maxLength": 180},
                "description": {"type": "string", "minLength": 1, "maxLength": 5000},
                "product": {"type": "string", "maxLength": 80},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
                "customer_name": {"type": "string", "maxLength": 120},
                "customer_contact": {"type": "string", "maxLength": 254},
            },
            "required": ["subject", "description"],
        },
    },
    {
        "name": "get_support_ticket",
        "description": "Read one support ticket by ID.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"ticket_id": {"type": "string"}},
                        "required": ["ticket_id"]},
    },
    {
        "name": "list_support_tickets",
        "description": "List recent tickets, optionally filtered by status.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "status": {"type": "string", "enum": ["", "open", "in_progress",
                                                                        "waiting_customer", "resolved", "closed"]},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                        },
    },
    {
        "name": "update_support_ticket_status",
        "description": "Update a ticket status and append an audit event.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "ticket_id": {"type": "string"},
                            "status": {"type": "string", "enum": ["open", "in_progress",
                                                                       "waiting_customer", "resolved", "closed"]},
                            "note": {"type": "string", "maxLength": 1000}},
                        "required": ["ticket_id", "status"]},
    },
    {
        "name": "prepare_staff_notification",
        "description": "Prepare and preview a staff notification for a ticket. Returns a short-lived confirmation token; does not send anything.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"ticket_id": {"type": "string"},
                                       "subject": {"type": "string", "maxLength": 180},
                                       "message": {"type": "string", "maxLength": 5000}},
                        "required": ["ticket_id"]},
    },
    {
        "name": "execute_staff_notification",
        "description": "Send the prepared notification only after the user reviewed its destination/content and explicitly approved it. In console mode this only writes a local event; SMTP mode sends real email to SUPPORT_STAFF_EMAIL.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"action_id": {"type": "string"},
                                       "confirmation_token": {"type": "string"}},
                        "required": ["action_id", "confirmation_token"]},
    },
]


class MCPServer:
    def __init__(self, actions=None, role=None, actor=None, tracer=None):
        self.actions = actions or SupportActions()
        self.role = role or os.environ.get("SUPPORT_MCP_ROLE", "agent")
        self.actor = actor or os.environ.get("SUPPORT_MCP_ACTOR", "mcp-client")
        self.tracer = tracer or TraceStore(Path(__file__).resolve().parent / "data" / "traces.jsonl")

    @staticmethod
    def result(value):
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
                "structuredContent": value}

    def call_tool(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("arguments phải là object.")
        permission = {"support_config_status": "ticket.read", "create_support_ticket": "ticket.write",
                      "get_support_ticket": "ticket.read", "list_support_tickets": "ticket.read",
                      "update_support_ticket_status": "ticket.write",
                      "prepare_staff_notification": "notification.prepare",
                      "execute_staff_notification": "notification.execute"}.get(name)
        if not permission: raise ValueError("Tool không tồn tại.")
        def evaluate(event):
            return {"passed": event["status"] == "ok", "checks": ["rbac", "argument_guard", "tool_completed"]}
        with self.tracer.span("tool." + name, self.actor, "tool", metadata=safe_metadata(arguments), evaluator=evaluate):
            require(self.role, permission); guard_tool_arguments(arguments)
            if name == "support_config_status": value = self.actions.notification_config()
            elif name == "create_support_ticket": value = self.actions.create_ticket(**arguments)
            elif name == "get_support_ticket": value = self.actions.get_ticket(**arguments)
            elif name == "list_support_tickets": value = self.actions.list_tickets(**arguments)
            elif name == "update_support_ticket_status": value = self.actions.update_ticket_status(**arguments)
            elif name == "prepare_staff_notification": value = self.actions.prepare_staff_notification(**arguments)
            else: value = self.actions.execute_staff_notification(**arguments)
        return self.result(value)

    def handle(self, request):
        method = request.get("method")
        if method == "initialize":
            return {"protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": "Create tickets locally. Prepare staff notifications first; execute only after explicit user approval."}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            params = request.get("params") or {}
            return self.call_tool(params.get("name"), params.get("arguments") or {})
        raise ValueError("Method không được hỗ trợ.")


def respond(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    server = MCPServer()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:  # MCP notifications do not receive responses.
                continue
            result = server.handle(request)
            respond({"jsonrpc": "2.0", "id": request["id"], "result": result})
        except Exception as exc:
            request_id = request.get("id") if isinstance(locals().get("request"), dict) else None
            respond({"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32000, "message": str(exc)}})
            traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    main()
