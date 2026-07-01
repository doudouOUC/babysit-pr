import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("poll.py")
MODULE_SPEC = importlib.util.spec_from_file_location("poll", MODULE_PATH)
poll = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
MODULE_SPEC.loader.exec_module(poll)


ACTIONS_URL = (
    "https://github.com/QwenLM/qwen-code/actions/runs/123456789/job/987654321"
)


class ParseWorkflowRunIdTest(unittest.TestCase):
    def test_extracts_run_id_from_actions_url(self):
        self.assertEqual(poll.parse_workflow_run_id(ACTIONS_URL), 123456789)

    def test_returns_none_for_non_actions_url(self):
        self.assertIsNone(
            poll.parse_workflow_run_id("https://app.codecov.io/gh/o/r/pull/1")
        )

    def test_returns_none_for_missing_url(self):
        self.assertIsNone(poll.parse_workflow_run_id(None))


class ParseJobIdTest(unittest.TestCase):
    def test_extracts_job_id_from_actions_url(self):
        self.assertEqual(poll.parse_job_id(ACTIONS_URL), 987654321)

    def test_returns_none_when_url_has_run_but_no_job(self):
        self.assertIsNone(
            poll.parse_job_id(
                "https://github.com/QwenLM/qwen-code/actions/runs/123456789"
            )
        )

    def test_returns_none_for_non_actions_url(self):
        self.assertIsNone(
            poll.parse_job_id("https://app.codecov.io/gh/o/r/pull/1")
        )

    def test_returns_none_for_missing_url(self):
        self.assertIsNone(poll.parse_job_id(None))


class DiffTest(unittest.TestCase):
    def test_only_new_failed_checks_are_reported(self):
        prior = {"checks_seen": [1]}
        current = {
            "comments": [],
            "reviews": [],
            "issue_comments": [],
            "failed_checks": [{"id": 1}, {"id": 2}],
        }
        delta = poll.diff(prior, current)
        self.assertEqual(
            [c["id"] for c in delta["new_failed_checks"]], [2]
        )


if __name__ == "__main__":
    unittest.main()
