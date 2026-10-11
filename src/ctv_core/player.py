"""Prompt for camera credentials and pipe native video and audio into mpv."""

import argparse
import getpass
import ipaddress
import math
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence

from . import AuthenticationError, CTVClient
from .flv import FLVWriter


def _mpv_command(executable: str) -> list[str]:
    return [
        executable,
        "--no-config",
        "--cache=no",
        "--cache-pause=no",
        "--input-terminal=no",
        "--force-window=yes",
        "--keep-open=no",
        "--idle=no",
        "--title=CTV camera",
        "--msg-level=all=warn",
        "--demuxer=lavf",
        "--demuxer-lavf-format=flv",
        "--demuxer-lavf-probe-info=yes",
        "--demuxer-lavf-probesize=33554432",
        "--demuxer-lavf-analyzeduration=0.2",
        "--demuxer-max-bytes=8MiB",
        "--demuxer-max-back-bytes=0",
        "--audio-buffer=0.1",
        "--vd-lavc-threads=1",
        "-",
    ]


class _PlaybackStopped(Exception):
    """The player closed its input while a container tag was being queued."""


class _MPVPipe:
    """Keep blocking pipe writes separate from the camera's keepalive owner."""

    def __init__(self, executable: str):
        self.stop = threading.Event()
        self._buffers: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self._write_error: OSError | None = None
        self.process = subprocess.Popen(
            _mpv_command(executable),
            stdin=subprocess.PIPE,
            bufsize=0,
        )
        self._writer = threading.Thread(target=self._write_buffers, daemon=True)
        self._watcher = threading.Thread(target=self._watch_process, daemon=True)
        self._writer.start()
        self._watcher.start()

    def _watch_process(self) -> None:
        self.process.wait()
        self.stop.set()

    def _write_buffers(self) -> None:
        pipe = self.process.stdin
        assert pipe is not None
        try:
            while not self.stop.is_set():
                try:
                    data = self._buffers.get(timeout=0.1)
                except queue.Empty:
                    continue
                remaining = memoryview(data)
                while remaining and not self.stop.is_set():
                    written = pipe.write(remaining)
                    if not written:
                        raise BrokenPipeError("mpv stopped reading its input pipe")
                    remaining = remaining[written:]
        except OSError as exc:
            self._write_error = exc
            self.stop.set()
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def write(self, data: bytes, *, poll: Callable[[], None]) -> bool:
        while not self.stop.is_set():
            # A paused player can fill the queue; service keepalive while waiting.
            poll()
            try:
                self._buffers.put(data, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def raise_if_failed(self) -> None:
        code = self.process.poll()
        if self._write_error is not None and code is None:
            try:
                code = self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    "Could not write media to mpv"
                ) from self._write_error
        if code not in (None, 0):
            raise RuntimeError(f"mpv exited with status {code}; see its output above")

    def close(self) -> None:
        self.stop.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        # Ending the process releases a writer blocked in the OS pipe.
        self._writer.join(timeout=2)
        self._watcher.join(timeout=2)

    def __enter__(self) -> "_MPVPipe":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def play_camera(
    host: str,
    user: str,
    password: str,
    *,
    port: int = 34567,
    channel: int = 0,
    stream: str = "Main",
    timeout: float = 5.0,
    mpv: str = "mpv",
) -> None:
    """Mux native H.264/G.711 into mpv stdin; closing mpv ends the session."""
    with CTVClient(
        host, user, password, port=port, channel=channel, stream=stream, timeout=timeout
    ) as client:
        print("Connected. Waiting for an H.264 keyframe...", flush=True)
        chunks = client.iter_chunks()
        for kind, first_frame, meta in chunks:
            if kind == "video" and meta.get("frame") == "I":
                break
        else:
            raise ConnectionError("Camera stream ended before the first keyframe")
        if meta.get("video_codec") != "h264":
            raise ValueError(
                f"Only H.264 video is supported; received {meta.get('video_codec')}"
            )
        print(
            "Playing video and audio in mpv. Close its window or press Ctrl+C to stop.",
            flush=True,
        )
        with _MPVPipe(mpv) as player:

            def write(data: bytes) -> None:
                if not player.write(data, poll=client.poll):
                    raise _PlaybackStopped()

            try:
                writer = FLVWriter(write)
                writer.write_chunk("video", first_frame, meta)
                for kind, data, meta in client.iter_chunks(stop=player.stop):
                    writer.write_chunk(kind, data, meta)
            except _PlaybackStopped:
                pass
            player.raise_if_failed()


def _ip_address(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "Enter a valid IPv4 or IPv6 camera address"
        ) from None


def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535")
    return number


def _channel(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("Channel must be an unsigned 32-bit integer")
    return number


def _timeout(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be finite and positive")
    return seconds


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Receive native XM/DVRIP camera video and audio and pipe FLV directly to mpv.",
    )
    parser.add_argument(
        "--host", type=_ip_address, help="camera IP address (prompted if omitted)"
    )
    parser.add_argument(
        "--user", help="camera login (prompted if omitted; default: admin)"
    )
    parser.add_argument(
        "--port", type=_port, default=34567, help="DVRIP port (default: 34567)"
    )
    parser.add_argument(
        "--channel", type=_channel, default=0, help="camera channel, starting at 0"
    )
    parser.add_argument(
        "--stream", choices=("Main", "Extra1", "Extra2"), default="Main"
    )
    parser.add_argument(
        "--timeout", type=_timeout, default=5.0, help="socket timeout in seconds"
    )
    parser.add_argument("--mpv", default="mpv", help="mpv executable name or path")
    args = parser.parse_args(argv)
    executable = shutil.which(args.mpv)
    if executable is None:
        parser.error("mpv was not found; install it or supply its path with --mpv")
    try:
        host = args.host or _ip_address(input("Camera IP: "))
        user = (
            args.user
            if args.user is not None
            else input("Camera login [admin]: ").strip() or "admin"
        )
        password = getpass.getpass("Camera password: ")
        play_camera(
            host,
            user,
            password,
            port=args.port,
            channel=args.channel,
            stream=args.stream,
            timeout=args.timeout,
            mpv=executable,
        )
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except EOFError:
        print("Input cancelled.", file=sys.stderr)
        return 1
    except AuthenticationError as exc:
        print(
            f"Authentication failed. Check the camera login and password. {exc}",
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
