"""Exercise the embedding API against a two-socket native DVR emulator."""

import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from ctv_core import AuthenticationError, CTVClient, StreamStopped
from ctv_core.protocol import (
    CLAIM,
    HEADER,
    KEEPALIVE,
    LOGIN,
    MEDIA,
    MONITOR,
    read_packet,
)

SESSION = 3


def packet(command, payload, *, session=SESSION):
    return HEADER.pack(255, 1, 0, 0, session, 0, command, len(payload)) + payload


def keyframe(data):
    stamp = (26 << 26) | (10 << 22) | (9 << 17) | (23 << 12) | (59 << 6) | 59
    return (
        b"\x00\x00\x01\xfd"
        + struct.pack("<BBBBII", 2, 25, 240, 135, stamp, len(data))
        + data
    )


def audio(data):
    return b"\x00\x00\x01\xf0" + struct.pack("<BBH", 14, 2, len(data)) + data


class NativeDVR:
    """Serve Login/Claim/Monitor/Keepalive through real local socket pairs."""

    def __init__(
        self,
        client=None,
        *,
        login_code=100,
        claim_code=100,
        start_code=100,
        stop_code=100,
    ):
        self.client = client or CTVClient("fixture", "admin", "password", timeout=3.0)
        self.login_code = login_code
        self.claim_code = claim_code
        self.start_code = start_code
        self.stop_code = stop_code
        self.control, self.control_peer = socket.socketpair()
        self.media, self.media_peer = socket.socketpair()
        for connection in (self.control, self.media):
            connection.settimeout(self.client.api.timeout)
        self.requests = []
        self.errors = []
        self.keepalive = threading.Event()
        self.shutdown = threading.Event()
        self.threads = []

    def __enter__(self):
        self.connection_patch = patch(
            "ctv_core.api.socket.create_connection",
            side_effect=[self.control, self.media],
        )
        self.connection_patch.start()
        for connection in (self.control_peer, self.media_peer):
            thread = threading.Thread(
                target=self.serve, args=(connection,), daemon=True
            )
            thread.start()
            self.threads.append(thread)
        return self

    def serve(self, connection):
        try:
            while not self.shutdown.is_set():
                request = read_packet(connection)
                self.requests.append(request)
                if request.command == LOGIN:
                    payload = struct.pack(
                        "<6I", 4, 2, self.login_code, SESSION, SESSION, 0
                    )
                else:
                    code = 100
                    if request.command == CLAIM:
                        code = self.claim_code
                    elif request.command == MONITOR:
                        action = struct.unpack("<8I", request.payload)[3]
                        code = self.stop_code if action == 1 else self.start_code
                    elif request.command == KEEPALIVE:
                        self.keepalive.set()
                    else:
                        raise AssertionError(f"Unexpected command: {request.command}")
                    payload = struct.pack("<4I", SESSION, code, 0, 0)
                connection.sendall(packet(request.command + 1, payload))
                if request.command == CLAIM:
                    return
        except (ConnectionError, OSError):
            # EOF is expected when the client releases its sockets.
            pass
        except BaseException as exc:
            self.errors.append(exc)

    def send(self, payload, *, command=MEDIA, session=SESSION):
        self.media_peer.sendall(packet(command, payload, session=session))

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.client.close()
        finally:
            self.shutdown.set()
            self.connection_patch.stop()
            for connection in (
                self.control,
                self.media,
                self.control_peer,
                self.media_peer,
            ):
                connection.close()
            for thread in self.threads:
                thread.join(2)
        if exc_type is None:
            if any(thread.is_alive() for thread in self.threads):
                raise AssertionError("Native fixture thread did not exit")
            if self.errors:
                raise self.errors[0]


class ClientTests(unittest.TestCase):
    def assert_closed(self, dvr):
        self.assertIsNone(dvr.client.api.sock)
        self.assertIsNone(dvr.client.media.media_sock)
        self.assertEqual((dvr.control.fileno(), dvr.media.fileno()), (-1, -1))

    def test_native_lifecycle_and_video_only_reader(self):
        client = CTVClient("fixture", "admin", "password", channel=7, stream="Extra1")
        with NativeDVR(client) as dvr:
            with client:
                dvr.send(
                    audio(b"sound")
                    + keyframe(b"first")
                    + audio(b"sound2")
                    + keyframe(b"second")
                )
                chunks = client.iter_video_chunks()
                self.assertEqual(next(chunks)[1], b"first")
                self.assertEqual(next(chunks)[1], b"second")
                with self.assertRaisesRegex(RuntimeError, "already open"):
                    client.open()
            client.close()
            self.assert_closed(dvr)
            self.assertEqual(
                [request.command for request in dvr.requests],
                [LOGIN, CLAIM, MONITOR, MONITOR],
            )
            login, claim, start, stop = dvr.requests
            self.assertEqual(login.session, 0)
            self.assertEqual(
                login.payload,
                b"admin".ljust(32, b"\x00")
                + b"mF95aD4o".ljust(32, b"\x00")
                + struct.pack("<II", 1, 2),
            )
            self.assertEqual(
                struct.unpack("<8I", claim.payload), (SESSION, 7, 0, 3, 0, 0, 0, 0)
            )
            self.assertEqual(
                struct.unpack("<8I", start.payload), (SESSION, 7, 0, 0, 0, 0, 0, 0)
            )
            self.assertEqual(
                struct.unpack("<8I", stop.payload), (SESSION, 7, 0, 1, 0, 0, 0, 0)
            )

    def test_fragmented_chunks_and_switching_readers_keep_all_siblings(self):
        video = b"\x00\x00\x00\x01\x65video\x00\x00\x01\xf0inside"
        info = b"\x00\x00\x01\xf9" + struct.pack("<BBH", 1, 0, 4) + b"info"
        delta = b"\x00\x00\x01\xfc" + struct.pack("<I", 5) + b"delta"
        with NativeDVR() as dvr, dvr.client as client:
            payload = info + keyframe(video) + audio(bytes(range(160))) + delta
            dvr.send(b"ignored", command=9999)
            # Native headers and frame bodies cross packet and TCP boundaries.
            for start, end in ((0, 2), (2, 15), (15, 32), (32, len(payload))):
                wire = packet(MEDIA, payload[start:end])
                for offset in range(0, len(wire), 7):
                    dvr.media_peer.sendall(wire[offset : offset + 7])
            first = client.read_chunk()
            chunks = client.iter_chunks()
            second = next(chunks)
            third = client.read_chunk()
            fourth = next(chunks)
            self.assertEqual(
                [(chunk[0], chunk[1]) for chunk in (first, second, third, fourth)],
                [
                    ("info", b"info"),
                    ("video", video),
                    ("audio", bytes(range(160))),
                    ("video", b"delta"),
                ],
            )
            self.assertEqual(second[2]["datetime"], "2026-10-09T23:59:59")
            self.assertEqual((second[2]["width"], second[2]["height"]), (1920, 1080))
            self.assertEqual(third[2]["audio_codec"], "g711a")
            self.assertEqual(third[2]["sample_rate"], 8000)
            self.assertEqual(fourth[2]["fps"], 25)

    def test_keepalive_is_serviced_while_waiting_for_a_partial_packet(self):
        with NativeDVR() as dvr, dvr.client as client:
            wire = packet(MEDIA, keyframe(b"delayed"))
            dvr.media_peer.sendall(wire[: HEADER.size + 3])
            sender = threading.Timer(
                1.2, dvr.media_peer.sendall, args=(wire[HEADER.size + 3 :],)
            )
            sender.start()
            try:
                self.assertEqual(client.read_chunk()[1], b"delayed")
                self.assertTrue(dvr.keepalive.is_set())
                keepalive = [
                    request for request in dvr.requests if request.command == KEEPALIVE
                ]
                self.assertEqual(
                    (keepalive[0].sequence, keepalive[0].payload), (0, b"")
                )
            finally:
                sender.cancel()
                sender.join(2)

    def test_stop_event_interrupts_a_partial_packet_and_closes_the_session(self):
        with NativeDVR() as dvr, dvr.client as client:
            dvr.media_peer.sendall(
                packet(MEDIA, keyframe(b"never-complete"))[: HEADER.size + 3]
            )
            stop = threading.Event()
            timer = threading.Timer(0.15, stop.set)
            timer.start()
            try:
                started = time.monotonic()
                self.assertEqual(list(client.iter_chunks(stop=stop)), [])
                self.assertLess(time.monotonic() - started, 1.0)
                self.assert_closed(dvr)
            finally:
                timer.cancel()
                timer.join(2)

    def test_read_chunk_reports_cancellation_before_returning_buffered_data(self):
        with NativeDVR() as dvr, dvr.client as client:
            dvr.send(keyframe(b"first") + audio(b"buffered"))
            self.assertEqual(client.read_chunk()[1], b"first")
            stop = threading.Event()
            stop.set()
            with self.assertRaises(StreamStopped):
                client.read_chunk(stop=stop)
            self.assert_closed(dvr)

    def test_login_claim_and_start_failures_release_created_sockets(self):
        for failed_stage in ("login", "claim", "start"):
            with (
                self.subTest(stage=failed_stage),
                NativeDVR(**{failed_stage + "_code": 106}) as dvr,
            ):
                exception = (
                    AuthenticationError if failed_stage == "login" else RuntimeError
                )
                with self.assertRaisesRegex(exception, "return code 106"):
                    dvr.client.open()
                self.assertIsNone(dvr.client.api.sock)
                self.assertIsNone(dvr.client.media.media_sock)
                self.assertEqual(dvr.control.fileno(), -1)
                if failed_stage != "login":
                    self.assertEqual(dvr.media.fileno(), -1)

    def test_stop_failure_still_closes_both_sockets(self):
        with NativeDVR(stop_code=106) as dvr:
            dvr.client.open()
            dvr.client.close()
            self.assert_closed(dvr)

    def test_timeout_eof_bad_session_and_bad_chunk_are_fatal(self):
        for failure in ("timeout", "eof", "session", "chunk"):
            client = CTVClient("fixture", timeout=0.15)
            with self.subTest(failure=failure), NativeDVR(client) as dvr, client:
                exception = TimeoutError
                if failure == "eof":
                    dvr.media_peer.sendall(packet(MEDIA, keyframe(b"partial"))[:-1])
                    dvr.media_peer.shutdown(socket.SHUT_WR)
                    exception = ConnectionError
                elif failure == "session":
                    dvr.send(keyframe(b"video"), session=SESSION + 1)
                    exception = RuntimeError
                elif failure == "chunk":
                    dvr.send(b"invalid-chunk")
                    exception = ValueError
                with self.assertRaises(exception):
                    client.read_chunk()
                self.assert_closed(dvr)

    def test_reopening_drops_previous_buffered_chunks(self):
        client = CTVClient("fixture")
        with NativeDVR(client) as first, client:
            first.send(keyframe(b"old") + audio(b"stale"))
            self.assertEqual(client.read_chunk()[1], b"old")
        with NativeDVR(client) as second, client:
            second.send(keyframe(b"new"))
            self.assertEqual(client.read_chunk()[1], b"new")

    def test_invalid_parameters_are_rejected_before_any_connection(self):
        with patch("ctv_core.api.socket.create_connection") as connect:
            for arguments in (
                {"port": 0},
                {"port": 65536},
                {"timeout": 0},
                {"timeout": float("inf")},
                {"timeout": float("nan")},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    CTVClient("fixture", **arguments)
            for arguments in (
                {"channel": -1},
                {"channel": 2**32},
                {"stream": "unknown"},
                {"native_stream_code": -1},
                {"native_stream_code": 2**32},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    CTVClient("fixture", **arguments).open()
            connect.assert_not_called()

    def test_context_cleanup_preserves_the_application_exception(self):
        with NativeDVR(stop_code=106) as dvr:
            with self.assertRaisesRegex(ValueError, "application failed"):
                with dvr.client:
                    raise ValueError("application failed")
            self.assert_closed(dvr)


if __name__ == "__main__":
    unittest.main()
