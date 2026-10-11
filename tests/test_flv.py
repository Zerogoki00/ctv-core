"""Validate FLV track payloads, shared timing and audio sample preservation."""

import io
import json
import shutil
import struct
import subprocess
import unittest

from ctv_core.flv import FLVWriter

SPS = b"\x67\x42\x00\x1e\xab\x40"
PPS = b"\x68\xce\x06\xe2"
VIDEO = b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x01" + PPS + b"\x00\x00\x01\x65video"
VIDEO_META = {
    "frame": "I",
    "video_codec": "h264",
    "fps": 25,
    "width": 1920,
    "height": 1080,
}
AUDIO_META = {"audio_codec": "g711a", "sample_rate": 8000}


def flv_tags(data):
    if data[:13] != b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00":
        raise AssertionError("Expected an FLV header advertising both video and audio")
    offset = 13
    tags = []
    while offset < len(data):
        header = data[offset : offset + 11]
        size = int.from_bytes(header[1:4], "big")
        timestamp = int.from_bytes(header[4:7], "big") | header[7] << 24
        end = offset + 11 + size
        if data[end : end + 4] != struct.pack(">I", 11 + size):
            raise AssertionError("Invalid FLV previous-tag size")
        tags.append((header[0], timestamp, data[offset + 11 : end]))
        offset = end + 4
    return tags


class FLVTests(unittest.TestCase):
    def test_tracks_share_origin_and_video_follows_audio_clock(self):
        output = io.BytesIO()
        writer = FLVWriter(output.write)
        writer.write_chunk("audio", b"discard", AUDIO_META)
        writer.write_chunk("video", b"discard", {"frame": "P"})
        writer.write_chunk("video", VIDEO, VIDEO_META)
        writer.write_chunk("audio", b"a" * 400, AUDIO_META)
        writer.write_chunk(
            "video", b"\x00\x00\x01\x41delta", dict(VIDEO_META, frame="P")
        )
        writer.write_chunk("audio", b"b" * 160, AUDIO_META)
        tags = flv_tags(output.getvalue())
        self.assertEqual(
            [(kind, stamp) for kind, stamp, _ in tags],
            [
                (18, 0),
                (9, 0),
                (9, 0),
                (8, 0),
                (9, 50),
                (8, 50),
            ],
        )
        self.assertEqual(tags[3][2], b"\x72" + b"a" * 400)
        self.assertEqual(tags[5][2], b"\x72" + b"b" * 160)
        self.assertEqual(writer.audio_samples, 560)
        self.assertEqual(writer.video_frames, 2)

    def test_mulaw_payload_is_preserved(self):
        output = io.BytesIO()
        writer = FLVWriter(output.write)
        writer.write_chunk("video", VIDEO, VIDEO_META)
        samples = bytes(range(256))
        writer.write_chunk("audio", samples, dict(AUDIO_META, audio_codec="g711u"))
        self.assertEqual(flv_tags(output.getvalue())[-1][2], b"\x82" + samples)

    def test_audio_format_changes_are_reported(self):
        writer = FLVWriter(lambda data: None)
        writer.write_chunk("video", VIDEO, VIDEO_META)
        with self.assertRaisesRegex(ValueError, "Unsupported audio format"):
            writer.write_chunk("audio", b"samples", dict(AUDIO_META, sample_rate=16000))
        writer.write_chunk("audio", b"samples", AUDIO_META)
        with self.assertRaisesRegex(ValueError, "Audio codec changed"):
            writer.write_chunk(
                "audio", b"samples", dict(AUDIO_META, audio_codec="g711u")
            )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required"
)
class FLVDecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        encoded = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1920x1080:rate=25",
                "-frames:v",
                "1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-f",
                "h264",
                "pipe:1",
            ],
            capture_output=True,
        )
        if encoded.returncode:
            raise unittest.SkipTest(
                "The test fixture requires FFmpeg's libx264 encoder"
            )
        cls.video = encoded.stdout

    def test_both_tracks_decode_after_large_keyframe_without_losing_audio(self):
        self.assertGreater(len(self.video), 65536)
        for codec, raw_format, decoded_codec in (
            ("g711a", "alaw", "pcm_alaw"),
            ("g711u", "mulaw", "pcm_mulaw"),
        ):
            with self.subTest(codec=codec):
                output = io.BytesIO()
                writer = FLVWriter(output.write)
                writer.write_chunk("video", self.video, VIDEO_META)
                samples = bytes(range(160)) * 32
                for offset in range(0, len(samples), 160):
                    writer.write_chunk(
                        "audio",
                        samples[offset : offset + 160],
                        dict(AUDIO_META, audio_codec=codec),
                    )
                flv = output.getvalue()
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_streams",
                        "-of",
                        "json",
                        "pipe:0",
                    ],
                    input=flv,
                    capture_output=True,
                    check=True,
                )
                streams = json.loads(probe.stdout)["streams"]
                self.assertEqual(
                    [track["codec_name"] for track in streams], ["h264", decoded_codec]
                )
                self.assertEqual(streams[0]["avg_frame_rate"], "25/1")
                self.assertEqual(streams[1]["sample_rate"], "8000")
                self.assertEqual(streams[1]["channels"], 1)
                original = subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-f",
                        raw_format,
                        "-ar",
                        "8000",
                        "-ac",
                        "1",
                        "-i",
                        "pipe:0",
                        "-f",
                        "s16le",
                        "pipe:1",
                    ],
                    input=samples,
                    capture_output=True,
                    check=True,
                ).stdout
                decoded = subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-xerror",
                        "-i",
                        "pipe:0",
                        "-map",
                        "0:a:0",
                        "-f",
                        "s16le",
                        "pipe:1",
                    ],
                    input=flv,
                    capture_output=True,
                    check=True,
                ).stdout
                self.assertEqual(decoded, original)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-xerror",
                        "-i",
                        "pipe:0",
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0",
                        "-f",
                        "null",
                        "-",
                    ],
                    input=flv,
                    capture_output=True,
                    check=True,
                )


if __name__ == "__main__":
    unittest.main()
