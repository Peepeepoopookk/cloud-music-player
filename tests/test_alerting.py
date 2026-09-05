import os
import unittest
from unittest.mock import patch, MagicMock
from dashboard.app import app
from scraper.alerting import send_alert


class AlertingTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    @patch.dict(os.environ, {}, clear=True)
    def test_send_alert_noop_when_env_unset(self):
        # When DISCORD_ALERT_WEBHOOK_URL is not set, must return False without making network calls
        result = send_alert("Test Alert", "Details here")
        self.assertFalse(result)

    @patch.dict(os.environ, {"DISCORD_ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123/abc"})
    @patch("scraper.alerting.requests.post")
    def test_send_alert_success_when_env_set(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_post.return_value = mock_response

        result = send_alert("Database Error", "Failed to write database.json", level="error")
        self.assertTrue(result)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://discord.com/api/webhooks/123/abc")
        self.assertIn("embeds", kwargs["json"])
        embed = kwargs["json"]["embeds"][0]
        self.assertIn("[ERROR] Database Error", embed["title"])
        self.assertEqual(embed["description"], "Failed to write database.json")

    @patch.dict(os.environ, {"DISCORD_ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123/abc"})
    @patch("scraper.alerting.requests.post")
    def test_send_alert_swallows_post_exceptions(self, mock_post):
        mock_post.side_effect = Exception("Connection refused / DNS lookup failure")

        # Must not raise an exception
        result = send_alert("Fatal Crash", "Something broke", level="error")
        self.assertFalse(result)

    @patch.dict(os.environ, {}, clear=True)
    def test_test_alert_endpoint_unconfigured(self):
        res = self.client.post("/api/tools/test-alert", json={"title": "Hello"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertFalse(data["webhook_configured"])
        self.assertFalse(data["sent"])

    @patch.dict(os.environ, {"DISCORD_ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123/abc"})
    @patch("scraper.alerting.requests.post")
    def test_test_alert_endpoint_configured(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        res = self.client.post("/api/tools/test-alert", json={"title": "Manual Test", "message": "Testing webhook"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["webhook_configured"])
        self.assertTrue(data["sent"])

    @patch.dict(os.environ, {"DISCORD_ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123/abc"})
    @patch("dashboard.app.send_alert")
    def test_flask_500_handler_fires_send_alert(self, mock_send_alert):
        mock_send_alert.return_value = True

        def trigger_error():
            raise RuntimeError("Simulated unhandled 500 error")

        original_view = app.view_functions.get("ping")
        app.view_functions["ping"] = trigger_error
        try:
            res = self.client.get("/ping")
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertEqual(data["error"], "Internal Server Error")
            mock_send_alert.assert_called_once()
            title, details = mock_send_alert.call_args[0][:2]
            self.assertIn("Unhandled Exception", title)
            self.assertIn("Simulated unhandled 500 error", details)
        finally:
            if original_view:
                app.view_functions["ping"] = original_view


if __name__ == "__main__":
    unittest.main()
