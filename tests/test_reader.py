import json
import tempfile
import unittest
from pathlib import Path
from session_search.reader import read_increment


def record(kind, **payload):
    return (json.dumps({"type": kind, "payload": payload}, ensure_ascii=False) + "\n").encode()


def message(role, text, **extra):
    return record("response_item", type="message", role=role,
                  content=[{"type": "input_text" if role == "user" else "output_text", "text": text}], **extra)


class ReaderTests(unittest.TestCase):
    def test_fork_keeps_own_identity_over_copied_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fork.jsonl'
            path.write_bytes(record('session_meta', id='fork', forked_from_id='parent') +
                             record('session_meta', id='parent') + message('user', 'Continue task'))
            result = read_increment(path)
            self.assertEqual(result['session']['id'], 'fork')
            self.assertEqual(result['chunks'][0]['session_id'], 'fork')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rollout.jsonl"
        self.meta = record("session_meta", id="session-1", cwd="/example", base_instructions="private")

    def test_incomplete_tail_and_restart(self):
        question = message("user", "Where is the pipeline?")
        answer = message("assistant", "In the repository.")
        self.path.write_bytes(self.meta + question + answer[:20])
        first = read_increment(self.path)
        self.assertEqual(first["next_offset"], len(self.meta + question))
        self.assertEqual(len(first["chunks"]), 1)
        with self.path.open("ab") as stream:
            stream.write(answer[20:])
        second = read_increment(self.path, first["next_offset"], json.loads(json.dumps(first["state"])))
        self.assertEqual(len(second["chunks"]), 1)
        self.assertIn("Where is the pipeline?", second["chunks"][0]["text"])
        whole = read_increment(self.path)
        self.assertEqual(list({c["id"]: c for c in first["chunks"] + second["chunks"]}.values()), whole["chunks"])

    def test_event_duplicates_tools_and_context_excluded(self):
        self.path.write_bytes(self.meta +
            message("developer", "secret developer") +
            message("user", "# AGENTS.md instructions for /example\n<INSTRUCTIONS>secret instructions</INSTRUCTIONS><environment_context>private cwd</environment_context>") +
            record("event_msg", type="user_message", message="actual request") +
            message("user", "actual request") +
            record("response_item", type="function_call_output", output="secret tool") +
            record("response_item", type="reasoning", summary="secret thoughts") +
            message("assistant", "internal", channel="analysis") +
            message("assistant", "actual answer") +
            record("event_msg", type="agent_message", message="actual answer"))
        result = read_increment(self.path)
        self.assertEqual(len(result["chunks"]), 1)
        self.assertEqual(len(result["chunks"][0]["sources"]), 2)
        self.assertNotIn("secret", str(result))
        self.assertNotIn("base_instructions", result["session"])

    def test_long_turn_bounded_and_stable(self):
        self.path.write_bytes(self.meta + record("event_msg", type="task_started", turn_id="turn-a") +
                              message("user", "question " * 100) + message("assistant", "answer 🌏 " * 1000))
        result = read_increment(self.path, max_chars=200)
        self.assertGreater(len(result["chunks"]), 10)
        self.assertTrue(all(len(c["text"]) <= 200 for c in result["chunks"]))
        self.assertTrue(all(c["turn_id"] == "turn-a" for c in result["chunks"]))
        self.assertEqual(result["chunks"], read_increment(self.path, max_chars=200)["chunks"])
        self.assertEqual(len({c["id"] for c in result["chunks"]}), len(result["chunks"]))

    def test_token_counter_and_interrupted_turn(self):
        self.path.write_bytes(self.meta + message("user", "pending request") + record("event_msg", type="turn_aborted"))
        first = read_increment(self.path, max_chars=100, max_tokens=80, token_counter=len)
        with self.path.open("ab") as stream:
            stream.write(message("assistant", "x" * 1000))
        second = read_increment(self.path, first["next_offset"], first["state"], max_chars=100, max_tokens=80, token_counter=len)
        self.assertTrue(all(len(c["text"]) <= 80 for c in second["chunks"]))
        self.assertIn("pending", second["chunks"][0]["text"])

    def test_malformed_complete_line_advances(self):
        self.path.write_bytes(self.meta + b"bad json\n" + message("user", "valid"))
        result = read_increment(self.path)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["next_offset"], self.path.stat().st_size)
        self.assertEqual(len(result["chunks"]), 1)

    def test_bounded_batches_equal_full_scan(self):
        self.path.write_bytes(self.meta + message("user", "question") + message("assistant", "answer"))
        expected = read_increment(self.path)["chunks"]
        chunks, offset, state = {}, 0, None
        for _ in range(10):
            batch = read_increment(self.path, offset, state, max_bytes=1)
            chunks.update({c["id"]: c for c in batch["chunks"]})
            if batch["next_offset"] == offset:
                break
            offset, state = batch["next_offset"], batch["state"]
        self.assertEqual(list(chunks.values()), expected)
        self.assertEqual(offset, self.path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
