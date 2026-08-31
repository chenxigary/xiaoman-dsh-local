import unittest

from bridge.livetalking_video import (
    install_video_codec_preference,
    prefer_video_codec,
    video_codecs_from_sdp,
)


class FakeCodec:
    def __init__(self, mime_type: str) -> None:
        self.mimeType = mime_type


class LiveTalkingVideoTest(unittest.TestCase):
    def test_preference_keeps_all_codecs_and_moves_requested_codec_first(self) -> None:
        codecs = [
            FakeCodec("video/VP8"),
            FakeCodec("video/rtx"),
            FakeCodec("video/H264"),
            FakeCodec("video/H264"),
        ]
        ordered = prefer_video_codec(codecs, "vp8")
        self.assertEqual(
            [codec.mimeType for codec in ordered],
            ["video/VP8", "video/H264", "video/H264", "video/rtx"],
        )
        self.assertCountEqual(ordered, codecs)

    def test_installer_is_idempotent_and_reorders_real_call(self) -> None:
        class FakeTransceiver:
            calls = []

            def setCodecPreferences(self, codecs):
                self.calls.append(list(codecs))
                return "ok"

        self.assertTrue(install_video_codec_preference(FakeTransceiver, "H264"))
        self.assertFalse(install_video_codec_preference(FakeTransceiver, "h264"))
        target = FakeTransceiver()
        result = target.setCodecPreferences([
            FakeCodec("video/VP8"),
            FakeCodec("video/H264"),
            FakeCodec("video/rtx"),
        ])
        self.assertEqual(result, "ok")
        self.assertEqual(
            [codec.mimeType for codec in target.calls[0]],
            ["video/H264", "video/VP8", "video/rtx"],
        )
        with self.assertRaises(RuntimeError):
            install_video_codec_preference(FakeTransceiver, "VP8")

    def test_rejects_unknown_codec(self) -> None:
        with self.assertRaises(ValueError):
            prefer_video_codec([], "AV1")

    def test_reads_negotiated_video_codec_order_from_sdp(self) -> None:
        sdp = "\r\n".join([
            "v=0",
            "m=audio 9 UDP/TLS/RTP/SAVPF 111",
            "a=rtpmap:111 opus/48000/2",
            "m=video 9 UDP/TLS/RTP/SAVPF 97 99 98",
            "a=rtpmap:97 VP8/90000",
            "a=rtpmap:98 rtx/90000",
            "a=rtpmap:99 H264/90000",
        ])
        self.assertEqual(video_codecs_from_sdp(sdp), ["VP8", "H264", "RTX"])


if __name__ == "__main__":
    unittest.main()
