import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from mcp_server import MCPServer
from observability import TraceStore
from support_actions import SupportActions


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch("mcp_server.guard_tool_arguments", return_value=True)
        self.guard.start(); self.addCleanup(self.guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        actions = SupportActions(Path(self.temp.name) / "support.sqlite3",
                                 {"SUPPORT_EMAIL_MODE": "console"})
        self.server = MCPServer(actions, role="admin",
                                tracer=TraceStore(Path(self.temp.name) / "traces.jsonl"))

    def tearDown(self):
        self.temp.cleanup()

    def test_initialize_and_tool_list(self):
        initialized = self.server.handle({"method": "initialize"})
        self.assertEqual(initialized["protocolVersion"], "2024-11-05")
        names = {tool["name"] for tool in self.server.handle({"method": "tools/list"})["tools"]}
        self.assertTrue({"create_support_ticket", "prepare_staff_notification",
                         "execute_staff_notification"} <= names)

    def test_ticket_and_two_step_notification_through_tools(self):
        created = self.server.call_tool("create_support_ticket", {
            "subject": "Cần hỗ trợ", "description": "Khách cần nhân viên gọi lại",
            "customer_contact": "0900000000"})["structuredContent"]
        prepared = self.server.call_tool("prepare_staff_notification",
                                         {"ticket_id": created["id"]})["structuredContent"]
        result = self.server.call_tool("execute_staff_notification", {
            "action_id": prepared["action_id"],
            "confirmation_token": prepared["confirmation_token"]})["structuredContent"]
        self.assertEqual(result["delivery"], "simulated")

    def test_agent_role_cannot_execute_notification(self):
        agent_server = MCPServer(self.server.actions, role="agent")
        with self.assertRaises(PermissionError):
            agent_server.call_tool("execute_staff_notification", {
                "action_id": "a", "confirmation_token": "b"})

    def test_stdio_json_rpc(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        process = subprocess.run([sys.executable, "mcp_server.py"],
                                 input="\n".join(json.dumps(r) for r in requests) + "\n",
                                 text=True, capture_output=True, check=True)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2])
        self.assertGreaterEqual(len(responses[1]["result"]["tools"]), 7)


if __name__ == "__main__":
    unittest.main()
