"""Exercise camera playback with parsed native chunks and real subprocess pipes."""

import contextlib
import io
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ctv_core import AuthenticationError, CTVClient, XMChunkParser
from ctv_core.player import _mpv_command, main, play_camera
from test_client import audio, keyframe
from test_flv import PPS, SPS, VIDEO, flv_tags


def delta(data):
    return b"\x00\x00\x01\xfc" + struct.pack("<I", len(data)) + data


def capture_tags(destination, count, *, delay=0):
    return (
        "import pathlib, sys, time; "
        f"time.sleep({delay}); "
        "stream = sys.stdin.buffer; data = bytearray(stream.read(13))\n"
        f"for _ in range({count}):\n"
        "    header = stream.read(11)\n"
        "    data.extend(header)\n"
        "    data.extend(stream.read(int.from_bytes(header[1:4], 'big') + 4))\n"
        f"pathlib.Path({str(destination)!r}).write_bytes(data)\n"
    )


class CameraFixture:
    """Exercise the client lifecycle with parsed chunks instead of live sockets."""

    def __init__(self, *, login_code=100):
        self.client = CTVClient("fixture", "admin", "password")
        self.client.api.alive_interval = 2
        self.keepalive = threading.Event()
        self._chunks = queue.Queue()
        self.login_code = login_code

    def __enter__(self):
        self._patches = contextlib.ExitStack()
        login_error = (
            AuthenticationError("DVR login rejected")
            if self.login_code != 100
            else None
        )
        self._patches.enter_context(
            patch.object(self.client.api, "login", side_effect=login_error)
        )
        self._patches.enter_context(
            patch.object(self.client.api, "keepalive", side_effect=self.keepalive.set)
        )
        self._patches.enter_context(patch.object(self.client.api, "close"))
        self._patches.enter_context(patch.object(self.client.media, "start"))
        self._patches.enter_context(patch.object(self.client.media, "stop"))
        self._patches.enter_context(patch.object(self.client.media, "close"))
        self._patches.enter_context(
            patch.object(
                self.client.media, "iter_chunks", return_value=self.iter_chunks()
            )
        )
        return self

    def send(self, payload):
        for chunk in XMChunkParser().feed(payload):
            self._chunks.put(chunk)

    def iter_chunks(self):
        while True:
            self.client.media.on_wait()
            try:
                yield self._chunks.get(timeout=0.05)
            except queue.Empty:
                continue

    def __exit__(self, exc_type, exc, traceback):
        self.client.close()
        self._patches.close()


class PlayerTests(unittest.TestCase):
    def play(self, dvr, code):
        with (
            patch("ctv_core.player.CTVClient", return_value=dvr.client),
            patch(
                "ctv_core.player._mpv_command",
                return_value=[sys.executable, "-c", code],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            play_camera("127.0.0.1", "admin", "password")

    def assert_closed(self, dvr):
        self.assertFalse(dvr.client._opened)
        self.assertIsNone(dvr.client.api.sock)
        self.assertIsNone(dvr.client.media.media_sock)
        dvr.client.media.start.assert_called_once_with(channel=0, stream="Main")
        dvr.client.media.stop.assert_called()
        dvr.client.api.close.assert_called()

    def test_pipe_receives_video_and_audio_starting_at_keyframe(self):
        first = VIDEO + bytes(range(256)) * 300
        second = b"\x00\x00\x00\x01\x41" + bytes(range(255, -1, -1)) * 400
        with tempfile.TemporaryDirectory() as directory, CameraFixture() as dvr:
            destination = Path(directory) / "stream.flv"
            dvr.send(
                delta(b"discard-before-keyframe")
                + audio(b"audio")
                + keyframe(first)
                + audio(b"more-audio")
                + delta(second)
            )
            self.play(dvr, capture_tags(destination, 5))
            tags = flv_tags(destination.read_bytes())
            self.assertEqual([tag[0] for tag in tags], [18, 9, 9, 8, 9])
            nals = [SPS, PPS, b"\x65video" + bytes(range(256)) * 300]
            self.assertEqual(
                tags[2][2],
                b"\x17\x01\x00\x00\x00"
                + b"".join(struct.pack(">I", len(nal)) + nal for nal in nals),
            )
            self.assertEqual(tags[3][2], b"\x72more-audio")
            self.assertEqual(
                tags[4][2],
                b"\x27\x01\x00\x00\x00"
                + struct.pack(">I", len(second) - 4)
                + second[4:],
            )
            self.assert_closed(dvr)

    def test_player_exit_cancels_an_idle_camera_read(self):
        with CameraFixture() as dvr:
            dvr.send(keyframe(VIDEO))
            started = time.monotonic()
            self.play(dvr, "import time; time.sleep(0.15)")
            self.assertLess(time.monotonic() - started, 1.5)
            self.assert_closed(dvr)

    def test_nonzero_player_exit_is_reported_and_closes_camera(self):
        with CameraFixture() as dvr:
            dvr.send(keyframe(VIDEO))
            with self.assertRaisesRegex(RuntimeError, "mpv exited with status 7"):
                self.play(dvr, "import sys; sys.exit(7)")
            self.assert_closed(dvr)

    def test_camera_failure_or_interrupt_terminates_player(self):
        for failure in (ConnectionError("Camera disconnected"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), CameraFixture() as dvr:

                def chunks():
                    yield XMChunkParser().feed(keyframe(VIDEO))[0]
                    raise failure

                processes = []
                real_popen = subprocess.Popen

                def launch(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process

                with (
                    patch.object(
                        dvr.client.media, "iter_chunks", return_value=chunks()
                    ),
                    patch("ctv_core.player.subprocess.Popen", side_effect=launch),
                ):
                    with self.assertRaises(type(failure)):
                        self.play(dvr, "import time; time.sleep(30)")
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(processes[0].stdin.closed)
                self.assert_closed(dvr)

    def test_keepalive_continues_when_player_does_not_read_pipe(self):
        frame = VIDEO + b"x" * 65536
        payload = keyframe(frame) + delta(frame) * 15
        with tempfile.TemporaryDirectory() as directory, CameraFixture() as dvr:
            destination = Path(directory) / "stream.flv"
            dvr.send(payload)
            self.play(dvr, capture_tags(destination, 18, delay=1.4))
            self.assertTrue(dvr.keepalive.is_set())
            frames = [
                payload
                for kind, _, payload in flv_tags(destination.read_bytes())
                if kind == 9 and payload[1] == 1
            ]
            nals = [SPS, PPS, b"\x65video" + b"x" * 65536]
            expected = b"\x17\x01\x00\x00\x00" + b"".join(
                struct.pack(">I", len(nal)) + nal for nal in nals
            )
            self.assertEqual(frames, [expected] * 16)
            self.assert_closed(dvr)

    def test_authentication_failure_does_not_start_player(self):
        with (
            CameraFixture(login_code=106) as dvr,
            patch("ctv_core.player.CTVClient", return_value=dvr.client),
            patch("ctv_core.player.subprocess.Popen") as popen,
        ):
            with self.assertRaises(AuthenticationError):
                play_camera("127.0.0.1", "admin", "wrong-password")
            popen.assert_not_called()
            self.assertIsNone(dvr.client.api.sock)
            self.assertIsNone(dvr.client.media.media_sock)

    def test_unsupported_codec_does_not_start_player(self):
        with CameraFixture() as dvr, patch("ctv_core.player.subprocess.Popen") as popen:
            frame = bytearray(keyframe(b"unsupported-video"))
            frame[4] = 3
            dvr.send(bytes(frame))
            with self.assertRaisesRegex(ValueError, "Only H.264 video is supported"):
                self.play(dvr, "")
            popen.assert_not_called()
            self.assert_closed(dvr)

    def test_interactive_credentials_use_hidden_password_prompt(self):
        with (
            patch("ctv_core.player.shutil.which", return_value="/usr/bin/mpv"),
            patch("builtins.input", side_effect=["192.0.2.10", ""]),
            patch("ctv_core.player.getpass.getpass", return_value="secret") as getpass,
            patch("ctv_core.player.play_camera") as play,
        ):
            self.assertEqual(main([]), 0)
            getpass.assert_called_once_with("Camera password: ")
            play.assert_called_once_with(
                "192.0.2.10",
                "admin",
                "secret",
                port=34567,
                channel=0,
                stream="Main",
                timeout=5.0,
                mpv="/usr/bin/mpv",
            )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("mpv"), "FFmpeg and mpv are required"
)
class PlayerDecodeTests(unittest.TestCase):
    def test_real_mpv_decodes_both_tracks_from_the_application_pipe(self):
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
            self.skipTest("The test fixture requires FFmpeg's libx264 encoder")
        self.assertGreater(len(encoded.stdout), 65536)
        command = _mpv_command(shutil.which("mpv"))
        command[-1:-1] = [
            "--vo=null",
            "--ao=null",
            "--frames=1",
            "--msg-level=all=info",
        ]
        with tempfile.TemporaryDirectory() as directory, CameraFixture() as camera:
            log_path = Path(directory) / "mpv.log"
            command[-1:-1] = [f"--log-file={log_path}"]
            camera.send((keyframe(encoded.stdout) + audio(bytes(range(160))) * 2) * 16)

            def chunks():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    camera.client.media.on_wait()
                    try:
                        yield camera._chunks.get(timeout=0.05)
                    except queue.Empty:
                        continue
                raise TimeoutError("mpv did not finish decoding both tracks")

            with (
                patch("ctv_core.player.CTVClient", return_value=camera.client),
                patch("ctv_core.player._mpv_command", return_value=command),
                patch.object(camera.client.media, "iter_chunks", return_value=chunks()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                play_camera("127.0.0.1", "admin", "password")
            log = log_path.read_text(errors="replace")
            self.assertIn("1920x1080", log)
            self.assertIn("pcm_alaw", log)
            self.assertIn("8000 Hz", log)
            self.assertIn("VO: [null]", log)
            self.assertIn("AO: [null]", log)
            self.assertFalse(camera.client._opened)


if __name__ == "__main__":
    unittest.main()
