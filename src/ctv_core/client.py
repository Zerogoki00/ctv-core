"""Synchronous embedding API with keepalive and explicit resource ownership."""

import logging
import time
from collections.abc import Iterator
from threading import Event

from .api import XMApiClient
from .media import Chunk, XMMediaStream

logger = logging.getLogger(__name__)


class StreamStopped(Exception):
    """The caller requested cancellation through a threading.Event."""


class CTVClient:
    """Own one control/media session, with all socket I/O in the caller's thread.

    Reading services keepalive, including while a partial packet is pending.
    Call poll() during long processing pauses to keep the session alive.
    Instances support one reader and must not issue concurrent control RPCs.
    """

    def __init__(
        self,
        host: str,
        user: str = "admin",
        password: str = "",
        *,
        port: int = 34567,
        timeout: float = 5.0,
        channel: int = 0,
        stream: str = "Main",
        native_stream_code: int | None = None,
    ):
        self.api = XMApiClient(host, port, user, password, timeout=timeout)
        self.media = XMMediaStream(self.api, native_stream_code=native_stream_code)
        self.channel = channel
        self.stream = stream
        self._opened = False
        self._next_keepalive = 0.0
        self._keepalive_interval = 0.0
        self._read_stop: Event | None = None

    def open(self) -> None:
        """Log in, claim the media socket and start the configured stream."""
        if self._opened:
            raise RuntimeError("The CTV client is already open")
        self.media.validate_start(self.channel, self.stream)
        try:
            self.api.login()
            self.media.start(channel=self.channel, stream=self.stream)
        except BaseException:
            self.close()
            raise
        self._keepalive_interval = max(1.0, min(8.0, self.api.alive_interval / 2))
        self._next_keepalive = time.monotonic() + self._keepalive_interval
        self._opened = True
        self.media.on_wait = self._on_wait

    def close(self) -> None:
        """Attempt native Stop and release both sockets, even after a failure."""
        self._opened = False
        self.media.on_wait = None
        try:
            self.media.stop()
        except (OSError, RuntimeError, ValueError) as exc:
            logger.debug("Native Stop failed during cleanup: %s", exc)
        finally:
            try:
                self.media.close()
            finally:
                self.api.close()

    def __enter__(self) -> "CTVClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def poll(self, *, stop: Event | None = None) -> None:
        """Check cancellation and send a keepalive when its deadline is due."""
        if not self._opened:
            raise RuntimeError("The CTV client is not open")
        if stop is not None and stop.is_set():
            raise StreamStopped("Native stream cancelled")
        if time.monotonic() >= self._next_keepalive:
            try:
                self.api.keepalive()
            except BaseException:
                self.close()
                raise
            self._next_keepalive = time.monotonic() + self._keepalive_interval

    def _on_wait(self) -> None:
        self.poll(stop=self._read_stop)

    def read_chunk(self, *, stop: Event | None = None) -> Chunk:
        """Read a complete video, audio or info chunk; fatal errors close the session.

        A set stop event raises StreamStopped and closes the session. A socket
        timeout, EOF or malformed packet is fatal because a partial DVRIP
        packet cannot be resumed after a failed read.
        """
        self._read_stop = stop
        try:
            self.poll(stop=stop)
            return next(self.media.iter_chunks())
        except StopIteration:
            self.close()
            raise ConnectionError("Native media stream ended") from None
        except BaseException:
            self.close()
            raise
        finally:
            self._read_stop = None

    def iter_chunks(self, *, stop: Event | None = None) -> Iterator[Chunk]:
        """Yield chunks in wire order; a stop event ends iteration normally."""
        while True:
            try:
                chunk = self.read_chunk(stop=stop)
            except StreamStopped:
                return
            yield chunk

    def iter_video_chunks(self, *, stop: Event | None = None) -> Iterator[Chunk]:
        """Yield video chunks while consuming accompanying audio/info packets."""
        for chunk in self.iter_chunks(stop=stop):
            if chunk[0] == "video":
                yield chunk
