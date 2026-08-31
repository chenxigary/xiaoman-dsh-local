import asyncio
import threading
import unittest

from avatar_relay import AvatarRelay, validate_avatar_base


class AvatarRelayTest(unittest.TestCase):
    def test_accepts_loopback_and_rejects_remote_or_paths(self) -> None:
        self.assertEqual(validate_avatar_base("http://127.0.0.1:8010/"), "http://127.0.0.1:8010")
        for value in ("https://127.0.0.1:8010", "http://example.com:8010", "http://127.0.0.1:8010/path"):
            with self.assertRaises(ValueError):
                validate_avatar_base(value)

    def test_unregister_is_compare_and_delete(self) -> None:
        relay = AvatarRelay("http://localhost:8010")
        relay.register("dsh-session:1", "123456")
        self.assertFalse(relay.unregister("dsh-session:1", "654321"))
        self.assertEqual(relay.avatar_session_for("dsh-session:1"), "123456")
        self.assertTrue(relay.unregister("dsh-session:1", "123456"))
        self.assertIsNone(relay.avatar_session_for("dsh-session:1"))

    def test_rejects_unbounded_or_structured_session_ids(self) -> None:
        relay = AvatarRelay("http://[::1]:8010")
        with self.assertRaises(ValueError):
            relay.register("../session", "123")
        with self.assertRaises(ValueError):
            relay.register("session", "bad/id")


class CapturingAvatarRelay(AvatarRelay):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:8010")
        self.packets: list[tuple[str, bytes, dict[str, str]]] = []

    def _post_wav(self, avatar_session_id: str, wav: bytes, metadata: dict[str, str] | None = None) -> None:
        self.packets.append((avatar_session_id, wav, dict(metadata or {})))


class AvatarRelayStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_packets_keep_sequence_and_pts_across_sentence_requests(self) -> None:
        relay = CapturingAvatarRelay()
        relay.register("dsh-session", "avatar-session")
        first = bytes(1600 * 2)  # 100 ms at 16 kHz
        second = bytes(3200 * 2)  # 200 ms

        self.assertTrue(await relay.forward_pcm(
            "dsh-session", first, turn_id="turn-1", generation=3,
        ))
        self.assertTrue(await relay.forward_pcm(
            "dsh-session", second, turn_id="turn-1", generation=3,
        ))

        first_headers = relay.packets[0][2]
        second_headers = relay.packets[1][2]
        self.assertEqual(first_headers["X-Xiaoman-Sequence"], "0")
        self.assertEqual(first_headers["X-Xiaoman-PTS-MS"], "0")
        self.assertEqual(first_headers["X-Xiaoman-Start"], "true")
        self.assertEqual(second_headers["X-Xiaoman-Sequence"], "1")
        self.assertEqual(second_headers["X-Xiaoman-PTS-MS"], "100")
        self.assertEqual(second_headers["X-Xiaoman-Start"], "false")

    async def test_new_generation_resets_stream_cursor(self) -> None:
        relay = CapturingAvatarRelay()
        relay.register("dsh-session", "avatar-session")
        pcm = bytes(320 * 2)
        await relay.forward_pcm("dsh-session", pcm, turn_id="turn", generation=1)
        await relay.forward_pcm("dsh-session", pcm, turn_id="turn", generation=2)
        headers = relay.packets[-1][2]
        self.assertEqual(headers["X-Xiaoman-Sequence"], "0")
        self.assertEqual(headers["X-Xiaoman-PTS-MS"], "0")
        self.assertEqual(headers["X-Xiaoman-Start"], "true")

    async def test_submit_pcm_returns_before_optional_upload_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingRelay(AvatarRelay):
            def _post_wav(self, avatar_session_id, wav, metadata=None):
                del avatar_session_id, wav, metadata
                started.set()
                release.wait(timeout=2)

        relay = BlockingRelay("http://127.0.0.1:8010")
        relay.register("dsh-session", "avatar-session")
        task = relay.submit_pcm(
            "dsh-session", bytes(320 * 2), turn_id="turn", generation=1,
        )
        self.assertIsNotNone(task)
        await asyncio.to_thread(started.wait, 1)
        self.assertFalse(task.done())
        release.set()
        self.assertTrue(await task)

    async def test_submit_pcm_serializes_packets_and_end_marker(self) -> None:
        relay = CapturingAvatarRelay()
        relay.register("dsh-session", "avatar-session")
        first = relay.submit_pcm(
            "dsh-session", bytes(1600 * 2), turn_id="turn", generation=1,
        )
        end = relay.submit_pcm(
            "dsh-session",
            bytes(320 * 2),
            turn_id="turn",
            generation=1,
            end=True,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(end)
        self.assertTrue(await first)
        self.assertTrue(await end)
        self.assertEqual(
            [packet[2]["X-Xiaoman-Sequence"] for packet in relay.packets],
            ["0", "1"],
        )
        self.assertEqual(
            [packet[2]["X-Xiaoman-End"] for packet in relay.packets],
            ["false", "true"],
        )

    async def test_reregister_drops_packets_queued_for_previous_owner(self) -> None:
        relay = CapturingAvatarRelay()
        relay.register("dsh-session", "old-avatar")
        stale = relay.submit_pcm(
            "dsh-session", bytes(320 * 2), turn_id="turn", generation=1,
        )
        self.assertIsNotNone(stale)
        self.assertTrue(relay.unregister("dsh-session", "old-avatar"))
        relay.register("dsh-session", "new-avatar")
        self.assertFalse(await stale)
        self.assertEqual(relay.packets, [])

    async def test_pcm_queue_is_bounded_and_reserves_end_marker(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingRelay(CapturingAvatarRelay):
            def __init__(self) -> None:
                AvatarRelay.__init__(
                    self, "http://127.0.0.1:8010", queue_size=3,
                )
                self.packets = []

            def _post_wav(self, avatar_session_id, wav, metadata=None):
                started.set()
                release.wait(timeout=2)
                super()._post_wav(avatar_session_id, wav, metadata)

        relay = BlockingRelay()
        relay.register("dsh-session", "avatar-session")
        first = relay.submit_pcm(
            "dsh-session", bytes(320 * 2), turn_id="turn", generation=1,
        )
        self.assertIsNotNone(first)
        await asyncio.to_thread(started.wait, 1)
        second = relay.submit_pcm(
            "dsh-session", bytes(320 * 2), turn_id="turn", generation=1,
        )
        dropped = relay.submit_pcm(
            "dsh-session", bytes(320 * 2), turn_id="turn", generation=1,
        )
        end = relay.submit_pcm(
            "dsh-session",
            bytes(320 * 2),
            turn_id="turn",
            generation=1,
            end=True,
        )
        self.assertIsNotNone(second)
        self.assertIsNone(dropped)
        self.assertIsNotNone(end)
        self.assertEqual(len(relay._tasks), 3)
        release.set()
        self.assertTrue(all(await asyncio.gather(first, second, end)))
        self.assertEqual(
            [packet[2]["X-Xiaoman-End"] for packet in relay.packets],
            ["false", "false", "true"],
        )


if __name__ == "__main__":
    unittest.main()
