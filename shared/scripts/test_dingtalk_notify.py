import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("dingtalk_notify.py")
MODULE_SPEC = importlib.util.spec_from_file_location("dingtalk_notify", MODULE_PATH)
dingtalk_notify = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
MODULE_SPEC.loader.exec_module(dingtalk_notify)


class DingTalkNotifyTest(unittest.TestCase):
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
        self.assertEqual(calls[0][1], 30)
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


if __name__ == "__main__":
    unittest.main()
