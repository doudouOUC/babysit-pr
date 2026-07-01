import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("dingtalk_notify.py")
MODULE_SPEC = importlib.util.spec_from_file_location("dingtalk_notify", MODULE_PATH)
dingtalk_notify = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
MODULE_SPEC.loader.exec_module(dingtalk_notify)


# ---------------------------------------------------------------------------
# OpenClaw transport tests (existing)
# ---------------------------------------------------------------------------

class OpenClawTransportTest(unittest.TestCase):
    def test_send_notification_uses_openclaw_channel_by_default(self):
        calls = []

        def fake_run(cmd, timeout_seconds):
            calls.append((cmd, timeout_seconds))
            return (
                0,
                '{"payload":{"result":{"messageId":"card_123"}}}',
                "",
            )

        result = dingtalk_notify.send_notification(
            title="PR fix pushed",
            text="A fix was pushed.",
            env={},
            command_runner=fake_run,
        )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["transport"], "openclaw")
        self.assertEqual(calls[0][1], 60)
        self.assertEqual(
            calls[0][0],
            [
                "openclaw",
                "message",
                "send",
                "--channel",
                "dingtalk-connector",
                "--target",
                "079458",
                "--message",
                "PR fix pushed\n\nA fix was pushed.",
                "--json",
            ],
        )

    def test_send_notification_can_override_openclaw_target(self):
        calls = []

        def fake_run(cmd, timeout_seconds):
            calls.append(cmd)
            return 0, '{"payload":{"result":{"messageId":"card_456"}}}', ""

        dingtalk_notify.send_notification(
            title="PR fix pushed",
            text="A fix was pushed.",
            env={
                "BABYSIT_PR_DINGTALK_OPENCLAW_CHANNEL": "dingtalk",
                "BABYSIT_PR_DINGTALK_OPENCLAW_TARGET": "user-123",
            },
            command_runner=fake_run,
        )

        self.assertIn("dingtalk", calls[0])
        self.assertIn("user-123", calls[0])

    def test_send_notification_reports_openclaw_failure(self):
        self.assertEqual(
            dingtalk_notify.send_notification(
                title="PR fix pushed",
                text="A fix was pushed.",
                env={},
                command_runner=lambda cmd, timeout_seconds: (
                    1,
                    "stdout detail",
                    "stderr detail",
                ),
            ),
            {
                "status": "failed",
                "transport": "openclaw",
                "exit_code": 1,
                "stderr": "stderr detail",
                "stdout": "stdout detail",
            },
        )


# ---------------------------------------------------------------------------
# Webhook transport tests
# ---------------------------------------------------------------------------

class WebhookTransportTest(unittest.TestCase):
    def test_webhook_sends_markdown_payload(self):
        posts = []

        def fake_poster(url, data, timeout):
            posts.append({"url": url, "data": json.loads(data), "timeout": timeout})
            return 200, '{"errcode": 0, "errmsg": "ok"}'

        result = dingtalk_notify.send_webhook_notification(
            title="PR #42 fix pushed",
            text="Fixed lint error.",
            env={"BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN": "abc123token"},
            http_poster=fake_poster,
        )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["transport"], "webhook")
        self.assertEqual(len(posts), 1)
        payload = posts[0]["data"]
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertEqual(payload["markdown"]["title"], "PR #42 fix pushed")
        self.assertIn("Fixed lint error.", payload["markdown"]["text"])
        self.assertIn("access_token=abc123token", posts[0]["url"])

    def test_webhook_computes_sign_when_secret_set(self):
        posts = []

        def fake_poster(url, data, timeout):
            posts.append({"url": url})
            return 200, '{"errcode": 0, "errmsg": "ok"}'

        dingtalk_notify.send_webhook_notification(
            title="test",
            text="hello",
            env={
                "BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN": "mytoken",
                "BABYSIT_PR_DINGTALK_WEBHOOK_SECRET": "SECabc123",
            },
            http_poster=fake_poster,
        )

        url = posts[0]["url"]
        self.assertIn("&timestamp=", url)
        self.assertIn("&sign=", url)

    def test_webhook_skips_sign_without_secret(self):
        posts = []

        def fake_poster(url, data, timeout):
            posts.append({"url": url})
            return 200, '{"errcode": 0, "errmsg": "ok"}'

        dingtalk_notify.send_webhook_notification(
            title="test",
            text="hello",
            env={"BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN": "mytoken"},
            http_poster=fake_poster,
        )

        url = posts[0]["url"]
        self.assertNotIn("&sign=", url)
        self.assertNotIn("&timestamp=", url)

    def test_webhook_skips_when_token_not_set(self):
        result = dingtalk_notify.send_webhook_notification(
            title="test",
            text="hello",
            env={},
            http_poster=lambda *a: (200, '{"errcode":0}'),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["transport"], "webhook")

    def test_webhook_reports_dingtalk_error(self):
        def fake_poster(url, data, timeout):
            return 200, '{"errcode": 300001, "errmsg": "invalid token"}'

        result = dingtalk_notify.send_webhook_notification(
            title="test",
            text="hello",
            env={"BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN": "badtoken"},
            http_poster=fake_poster,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["transport"], "webhook")
        self.assertEqual(result["errcode"], 300001)
        self.assertEqual(result["errmsg"], "invalid token")

    def test_webhook_reports_network_error(self):
        def fake_poster(url, data, timeout):
            return -1, "Connection refused"

        result = dingtalk_notify.send_webhook_notification(
            title="test",
            text="hello",
            env={"BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN": "mytoken"},
            http_poster=fake_poster,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], -1)


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------

class DispatchTest(unittest.TestCase):
    def test_dispatch_prefers_webhook_over_openclaw(self):
        webhook_calls = []
        openclaw_calls = []

        def fake_poster(url, data, timeout):
            webhook_calls.append(url)
            return 200, '{"errcode": 0, "errmsg": "ok"}'

        def fake_run(cmd, timeout_seconds):
            openclaw_calls.append(cmd)
            return 0, '{"payload":{"result":{}}}', ""

        result = dingtalk_notify.send_notification(
            title="test",
            text="hello",
            env={
                "BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN": "mytoken",
                "BABYSIT_PR_DINGTALK_OPENCLAW_TARGET": "079458",
            },
            command_runner=fake_run,
            http_poster=fake_poster,
        )

        self.assertEqual(result["transport"], "webhook")
        self.assertEqual(len(webhook_calls), 1)
        self.assertEqual(len(openclaw_calls), 0)

    def test_dispatch_falls_back_to_openclaw_without_webhook_token(self):
        def fake_run(cmd, timeout_seconds):
            return 0, '{"payload":{"result":{"messageId":"card_1"}}}', ""

        result = dingtalk_notify.send_notification(
            title="test",
            text="hello",
            env={},
            command_runner=fake_run,
        )

        self.assertEqual(result["transport"], "openclaw")
        self.assertEqual(result["status"], "sent")


# ---------------------------------------------------------------------------
# Utility tests
# ---------------------------------------------------------------------------

class UtilityTest(unittest.TestCase):
    def test_mask_token_short(self):
        self.assertEqual(dingtalk_notify.mask_token("ab"), "***")
        self.assertEqual(dingtalk_notify.mask_token(""), "***")
        self.assertEqual(dingtalk_notify.mask_token(None), "***")

    def test_mask_token_long(self):
        self.assertEqual(
            dingtalk_notify.mask_token("abcdefghij"),
            "abc...hij",
        )

    def test_compute_sign_returns_url_encoded_string(self):
        sign = dingtalk_notify.compute_sign(1234567890000, "SECtest")
        self.assertIsInstance(sign, str)
        self.assertTrue(len(sign) > 0)


if __name__ == "__main__":
    unittest.main()
