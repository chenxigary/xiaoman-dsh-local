from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents.codex.persona import (
    XIAOMAN_DEVELOPER_INSTRUCTIONS,
    developer_instructions_for,
)
from agents.codex.thread_manager import ThreadManager, ThreadMappingStore


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        thread_id = str(params.get("threadId") or "thread-persona")
        return {"thread": {"id": thread_id}}


class CodexPersonaTests(unittest.IsolatedAsyncioTestCase):
    def test_personas_are_fixed_and_unknown_characters_fail_closed(self) -> None:
        self.assertEqual(developer_instructions_for("xiaoman"), XIAOMAN_DEVELOPER_INSTRUCTIONS)
        self.assertIsNone(developer_instructions_for("default"))
        with self.assertRaisesRegex(ValueError, "unsupported character"):
            developer_instructions_for("attacker-controlled")

    async def test_thread_start_sets_xiaoman_and_resume_can_clear_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = _RecordingClient()
            store = ThreadMappingStore(Path(directory) / "threads.json")
            manager = ThreadManager(client, store)

            thread_id = await manager.ensure_thread(
                "session-persona",
                developer_instructions=XIAOMAN_DEVELOPER_INSTRUCTIONS,
            )
            self.assertEqual(thread_id, "thread-persona")
            self.assertEqual(client.calls[0][0], "thread/start")
            self.assertEqual(
                client.calls[0][1]["developerInstructions"],
                XIAOMAN_DEVELOPER_INSTRUCTIONS,
            )

            await manager.commit_thread("session-persona", thread_id)
            resumed = await manager.ensure_thread(
                "session-persona",
                developer_instructions=None,
            )
            self.assertEqual(resumed, thread_id)
            self.assertEqual(client.calls[1][0], "thread/resume")
            self.assertIsNone(client.calls[1][1]["developerInstructions"])


if __name__ == "__main__":
    unittest.main()
