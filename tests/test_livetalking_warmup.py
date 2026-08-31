from __future__ import annotations

from types import SimpleNamespace
import unittest

from bridge.livetalking_warmup import (
    install_session_warmup,
    warm_wav2lip_session,
)


class FakeArray:
    def __init__(self, shape, dtype):
        self.shape = shape
        self.dtype = dtype

    def copy(self):
        return FakeArray(self.shape, self.dtype)


class FakeNumpy:
    float32 = "float32"

    @staticmethod
    def ones(shape, dtype):
        return FakeArray(shape, dtype)


class FakeSessionManager:
    def init_builder(self, build_session_fn):
        self.build_session_fn = build_session_fn


class FakeAvatar:
    def __init__(self, *, model: str = "wav2lip", fail: bool = False):
        self.opt = SimpleNamespace(model=model)
        self.batch_size = 1
        self.fail = fail
        self.calls = []

    def inference_batch(self, index, mel):
        self.calls.append((index, mel.copy()))
        if self.fail:
            raise RuntimeError("synthetic warm-up failure")
        return object()


class LiveTalkingWarmupTests(unittest.TestCase):
    @staticmethod
    def _warm(avatar):
        return warm_wav2lip_session(avatar, numpy_module=FakeNumpy)

    def test_real_wav2lip_shape_and_telemetry(self):
        avatar = FakeAvatar()

        elapsed_ms = warm_wav2lip_session(avatar, numpy_module=FakeNumpy)

        self.assertIsNotNone(elapsed_ms)
        self.assertGreaterEqual(elapsed_ms, 0.0)
        self.assertEqual(len(avatar.calls), 1)
        index, mel = avatar.calls[0]
        self.assertEqual(index, 0)
        self.assertEqual(mel.shape, (1, 80, 16))
        self.assertEqual(mel.dtype, FakeNumpy.float32)
        self.assertEqual(avatar._xiaoman_session_warmup_ms, elapsed_ms)

    def test_non_wav2lip_session_is_untouched(self):
        avatar = FakeAvatar(model="musetalk")

        self.assertIsNone(
            warm_wav2lip_session(avatar, numpy_module=FakeNumpy)
        )
        self.assertEqual(avatar.calls, [])

    def test_installer_is_idempotent_and_primes_built_session(self):
        manager_class = type("Manager", (FakeSessionManager,), {})
        self.assertTrue(
            install_session_warmup(manager_class, warmup_fn=self._warm)
        )
        self.assertFalse(
            install_session_warmup(manager_class, warmup_fn=self._warm)
        )
        manager = manager_class()
        built = []

        def build(sessionid, params):
            avatar = FakeAvatar()
            built.append((sessionid, params, avatar))
            return avatar

        manager.init_builder(build)
        avatar = manager.build_session_fn("session-1", {"avatar": "xiaoman"})

        self.assertIs(avatar, built[0][2])
        self.assertEqual(built[0][:2], ("session-1", {"avatar": "xiaoman"}))
        self.assertEqual(len(avatar.calls), 1)

    def test_warmup_failure_does_not_make_offer_builder_fail(self):
        manager_class = type("Manager", (FakeSessionManager,), {})
        install_session_warmup(manager_class, warmup_fn=self._warm)
        manager = manager_class()
        avatar = FakeAvatar(fail=True)
        manager.init_builder(lambda _sessionid, _params: avatar)

        with self.assertLogs("bridge.livetalking_warmup", level="ERROR"):
            result = manager.build_session_fn("session-1", {})

        self.assertIs(result, avatar)
        self.assertEqual(len(avatar.calls), 1)


if __name__ == "__main__":
    unittest.main()
