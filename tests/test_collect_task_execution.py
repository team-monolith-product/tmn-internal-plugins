from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "tmn-operating"
    / "hooks"
    / "collect_task_execution.py"
)
SPEC = importlib.util.spec_from_file_location("collect_task_execution", SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(collector)


def json_line(value: dict) -> bytes:
    return (json.dumps(value) + "\n").encode()


class CollectTaskExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_plugin_data = os.environ.get("PLUGIN_DATA")
        os.environ["PLUGIN_DATA"] = str(Path(self.temporary.name) / "state")

    def tearDown(self) -> None:
        if self.previous_plugin_data is None:
            os.environ.pop("PLUGIN_DATA", None)
        else:
            os.environ["PLUGIN_DATA"] = self.previous_plugin_data
        self.temporary.cleanup()

    def test_codex_collects_root_and_subagent_usage(self) -> None:
        session_id = "root-session"
        day = datetime.now(timezone.utc)
        session_dir = (
            Path(self.temporary.name)
            / ".codex"
            / "sessions"
            / f"{day:%Y}"
            / f"{day:%m}"
            / f"{day:%d}"
        )
        session_dir.mkdir(parents=True)
        root = session_dir / "rollout-root.jsonl"
        root.write_bytes(
            json_line(
                {
                    "type": "session_meta",
                    "payload": {
                        "session_id": session_id,
                        "thread_source": "user",
                    },
                }
            )
        )
        root_offset = root.stat().st_size
        with root.open("ab") as stream:
            stream.write(
                json_line(
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-root", "effort": "high"},
                    }
                )
            )
            stream.write(
                json_line(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 40,
                                    "output_tokens": 20,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 120,
                                }
                            },
                        },
                    }
                )
            )

        subagent = session_dir / "rollout-subagent.jsonl"
        subagent.write_bytes(
            json_line(
                {
                    "type": "session_meta",
                    "payload": {
                        "session_id": session_id,
                        "thread_source": "subagent",
                    },
                }
            )
            + json_line(
                {
                    "type": "turn_context",
                    "payload": {"model": "gpt-sub", "effort": "medium"},
                }
            )
            + json_line(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 60,
                                "output_tokens": 10,
                                "total_tokens": 70,
                            }
                        },
                    },
                }
            )
        )

        payload = {
            "session_id": session_id,
            "transcript_path": str(root),
            "model": "fallback",
            "tool_input": {"list_url": "https://slack.example/list/row"},
        }
        collector.write_json(
            collector.task_state_path(payload, payload["tool_input"]["list_url"]),
            {
                "captured_at": time.time() - 5,
                "offsets": {str(root): root_offset},
                "model": "fallback",
                "execution_id": "run-test",
            },
        )

        metadata = collector.collect_publish_metadata(payload)

        self.assertEqual(metadata["total_tokens"], 190)
        self.assertEqual(metadata["model"], "gpt-root")
        self.assertEqual(metadata["reasoning_effort"], "high")
        self.assertEqual(metadata["collection_status"], "complete")

    def test_claude_deduplicates_streamed_messages(self) -> None:
        root = Path(self.temporary.name) / ".claude" / "projects" / "task.jsonl"
        root.parent.mkdir(parents=True)
        root.write_bytes(json_line({"type": "system"}))
        offset = root.stat().st_size
        assistant = {
            "type": "assistant",
            "effort": "max",
            "message": {
                "id": "message-1",
                "model": "claude-test",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 7,
                    "output_tokens_details": {"thinking_tokens": 3},
                },
            },
        }
        with root.open("ab") as stream:
            stream.write(json_line(assistant))
            stream.write(json_line(assistant))

        groups, malformed = collector.collect_claude([root], {str(root): offset}, None)

        self.assertFalse(malformed)
        self.assertEqual(groups, {collector.UsageKey("claude-test", "max", False): 42})
        self.assertIsNone(collector.claude_total_tokens({}))
        self.assertIsNone(collector.claude_total_tokens({"input_tokens": "unknown"}))

    def test_missing_usage_is_unavailable_not_zero(self) -> None:
        transcript = Path(self.temporary.name) / "rollout.jsonl"
        transcript.write_bytes(
            json_line(
                {
                    "type": "session_meta",
                    "payload": {"session_id": "empty", "thread_source": "user"},
                }
            )
        )
        payload = {
            "session_id": "empty",
            "transcript_path": str(transcript),
            "model": "gpt-test",
            "tool_input": {"list_url": "https://slack.example/list/row"},
        }

        metadata = collector.collect_publish_metadata(payload)

        self.assertIsNone(metadata["total_tokens"])
        self.assertEqual(metadata["collection_status"], "unavailable")

    def test_publish_retry_is_denied_without_auto_approval(self) -> None:
        output = collector.deny_for_retry({"execution_id": "run-test"})
        hook_output = output["hookSpecificOutput"]

        self.assertEqual(hook_output["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", hook_output)

    def test_already_enriched_publish_is_not_recollected(self) -> None:
        payload = {
            "tool_input": {
                "collector_version": collector.COLLECTOR_VERSION,
                "execution_id": "run-test",
            }
        }

        self.assertIsNone(collector.collect_publish_metadata(payload))

    def test_resumed_run_in_same_session_gets_a_new_execution_id(self) -> None:
        payload = {"session_id": "same-session", "transcript_path": "/tmp/codex"}
        first = collector.task_execution_id(
            payload, "https://slack.example/list/row", {"captured_at": 100.0}
        )
        retry = collector.task_execution_id(
            payload, "https://slack.example/list/row", {"captured_at": 100.0}
        )
        resumed = collector.task_execution_id(
            payload, "https://slack.example/list/row", {"captured_at": 200.0}
        )

        self.assertEqual(first, retry)
        self.assertNotEqual(first, resumed)


if __name__ == "__main__":
    unittest.main()
