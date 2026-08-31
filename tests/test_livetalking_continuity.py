import queue
import unittest

from bridge.livetalking_continuity import ContinuityQueue, install_livetalking_continuity


class FakeFrame:
    def __init__(self, value: str) -> None:
        self.value = value


class ContinuityQueueTest(unittest.TestCase):
    def test_audio_fallback_starts_only_after_first_real_frame(self) -> None:
        target = ContinuityQueue(
            4,
            generation=lambda: 7,
            silent_audio=lambda: FakeFrame("silence"),
        )
        with self.assertRaises(queue.Empty):
            target.get_nowait()
        target.put_nowait((FakeFrame("real"), None, 7))
        self.assertEqual(target.get_nowait()[0].value, "real")
        frame, eventpoint, generation = target.get_nowait()
        self.assertEqual(frame.value, "silence")
        self.assertIsNone(eventpoint)
        self.assertEqual(generation, 7)
        self.assertEqual(target.fallback_frames, 1)

    def test_real_audio_always_wins_over_fallback(self) -> None:
        target = ContinuityQueue(
            4,
            generation=lambda: 3,
            silent_audio=lambda: FakeFrame("silence"),
        )
        target.put_nowait((FakeFrame("real"), {"status": "start"}, 3))
        real = target.get_nowait()
        self.assertEqual(real[0].value, "real")
        fallback = target.get_nowait()
        self.assertEqual(fallback[0].value, "silence")
        self.assertIsNone(fallback[1])
        self.assertEqual(fallback[2], 3)

    def test_installer_is_idempotent_and_preserves_queue_bound(self) -> None:
        class FakeAudioFrame:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.planes = [self]
                self.sample_rate = None

            def update(self, value: bytes) -> None:
                self.value = value

        class FakeTrack:
            def __init__(self, player, kind) -> None:
                del player
                self.kind = kind
                self._generation = 5
                self._queue = queue.Queue(maxsize=9)

            @property
            def generation(self) -> int:
                return self._generation

            def clear_queue(self) -> int:
                drained = 0
                while True:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        return drained
                    drained += 1
                    self._queue.task_done()

        class FakeModule:
            PlayerStreamTrack = FakeTrack
            AUDIO_PTIME = 0.02
            SAMPLE_RATE = 16000
            AudioFrame = FakeAudioFrame

        self.assertTrue(install_livetalking_continuity(FakeModule))
        self.assertFalse(install_livetalking_continuity(FakeModule))
        track = FakeModule.PlayerStreamTrack(None, "audio")
        self.assertIsInstance(track._queue, ContinuityQueue)
        self.assertEqual(track._queue.maxsize, 9)
        with self.assertRaises(queue.Empty):
            track._queue.get_nowait()
        track._queue.put_nowait((FakeFrame("real"), None, 5))
        track._queue.get_nowait()
        frame, _, generation = track._queue.get_nowait()
        self.assertEqual(len(frame.value), 640)
        self.assertEqual(frame.sample_rate, 16000)
        self.assertEqual(generation, 5)
        # Generation changes call this while continuity fallback is armed.
        # It must terminate instead of manufacturing silence forever.
        self.assertEqual(track.clear_queue(), 0)
        track._queue.put_nowait((FakeFrame("queued"), None, 5))
        self.assertEqual(track.clear_queue(), 1)
        video_track = FakeModule.PlayerStreamTrack(None, "video")
        self.assertIsInstance(video_track._queue, queue.Queue)
        self.assertNotIsInstance(video_track._queue, ContinuityQueue)

if __name__ == "__main__":
    unittest.main()
