import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from support_actions import SupportActions


class SupportActionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "support.sqlite3"
        self.actions = SupportActions(self.db, {"SUPPORT_EMAIL_MODE": "console"})

    def tearDown(self):
        self.temp.cleanup()

    def ticket(self):
        return self.actions.create_ticket("Máy không lên nguồn", "Đã thử bộ sạc khác.",
                                          product="LAP-A14", priority="high")

    def test_create_get_list_and_update_ticket(self):
        ticket = self.ticket()
        self.assertEqual(self.actions.get_ticket(ticket["id"])["subject"], "Máy không lên nguồn")
        self.assertEqual(len(self.actions.list_tickets(status="open")), 1)
        updated = self.actions.update_ticket_status(ticket["id"], "in_progress", "Đã nhận")
        self.assertEqual(updated["status"], "in_progress")

    def test_prepare_does_not_send_and_requires_correct_token(self):
        prepared = self.actions.prepare_staff_notification(self.ticket()["id"])
        self.assertTrue(prepared["confirmation_required"])
        with self.assertRaises(ValueError):
            self.actions.execute_staff_notification(prepared["action_id"], "wrong")
        result = self.actions.execute_staff_notification(prepared["action_id"],
                                                         prepared["confirmation_token"])
        self.assertEqual(result["delivery"], "simulated")
        with self.assertRaises(ValueError):
            self.actions.execute_staff_notification(prepared["action_id"],
                                                    prepared["confirmation_token"])

    def test_smtp_mode_uses_fixed_staff_destination(self):
        env = {"SUPPORT_EMAIL_MODE": "smtp", "SUPPORT_SMTP_HOST": "smtp.example.test",
               "SUPPORT_SMTP_PORT": "587", "SUPPORT_SMTP_SECURITY": "starttls",
               "SUPPORT_SMTP_USERNAME": "agent", "SUPPORT_SMTP_PASSWORD": "secret",
               "SUPPORT_EMAIL_FROM": "bot@example.test",
               "SUPPORT_STAFF_EMAIL": "staff@example.test"}
        actions = SupportActions(self.db, env)
        prepared = actions.prepare_staff_notification(self.ticket()["id"])
        client = MagicMock()
        client.__enter__.return_value = client
        with patch("support_actions.smtplib.SMTP", return_value=client) as smtp:
            result = actions.execute_staff_notification(prepared["action_id"],
                                                        prepared["confirmation_token"])
        smtp.assert_called_once_with("smtp.example.test", 587, timeout=20)
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("agent", "secret")
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "staff@example.test")
        self.assertEqual(result["delivery"], "sent")

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            self.actions.create_ticket("", "description")
        with self.assertRaises(ValueError):
            self.actions.create_ticket("subject", "description", priority="critical")
        with self.assertRaises(ValueError):
            self.actions.list_tickets(limit=0)


if __name__ == "__main__":
    unittest.main()
