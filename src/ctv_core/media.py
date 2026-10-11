"""Native OPMonitor handshake and incremental video/audio chunk parsing."""

import logging
import socket
import struct
from collections.abc import Callable, Iterator

from .api import XMApiClient
from .protocol import (
    CLAIM,
    MAX_PAYLOAD_SIZE,
    MEDIA,
    MONITOR,
    decode_dvr_datetime,
    read_packet,
)

logger = logging.getLogger(__name__)
Chunk = tuple[str, bytes, dict]
SAMPLE_RATES = (4000, 6000, 8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000)
AUDIO_CODECS = {0x0E: "g711a", 0x21: "g711u"}
STREAM_CODES = {"Main": 2, "Extra1": 3, "Extra2": 4}


class XMChunkParser:
    """Keep incomplete signatures, headers and bodies across media packets."""

    def __init__(self):
        self.buffer = bytearray()
        self.video_meta: dict = {}

    def feed(self, payload: bytes) -> list[Chunk]:
        self.buffer.extend(payload)
        offset = 0
        chunks = []
        while len(self.buffer) - offset >= 4:
            if self.buffer[offset : offset + 3] != b"\x00\x00\x01":
                raise ValueError(
                    f"Invalid native chunk boundary: {self.buffer[offset:offset+16].hex()}"
                )
            tag = self.buffer[offset + 3]
            if tag not in (0xFD, 0xFC, 0xF0, 0xFA, 0xF9):
                raise ValueError(f"Unsupported native chunk tag: 0x{tag:02x}")
            header_size = 16 if tag == 0xFD else 8
            if len(self.buffer) - offset < header_size:
                break
            meta = {"tag": f"0x{tag:02x}"}
            if tag == 0xFD:
                codec, fps, width, height, stamp, size = struct.unpack_from(
                    "<BBBBII",
                    self.buffer,
                    offset + 4,
                )
                video_meta = {
                    "video_codec_id": codec,
                    "video_codec": "h264" if codec == 2 else f"unknown-video-{codec}",
                    "fps": fps,
                    "width": width * 8,
                    "height": height * 8,
                }
                date = decode_dvr_datetime(stamp)
                meta.update(video_meta)
                meta.update(
                    frame="I",
                    timestamp_raw=stamp,
                    datetime=date.isoformat() if date is not None else None,
                )
                kind = "video"
            elif tag == 0xFC:
                size = struct.unpack_from("<I", self.buffer, offset + 4)[0]
                meta.update(self.video_meta)
                meta["frame"] = "P"
                kind = "video"
            else:
                codec, rate, size = struct.unpack_from("<BBH", self.buffer, offset + 4)
                kind = "info" if tag == 0xF9 else "audio"
                if kind == "audio":
                    meta.update(
                        audio_codec_id=codec,
                        audio_codec=AUDIO_CODECS.get(codec, f"unknown-audio-{codec}"),
                        sample_rate_code=rate,
                        sample_rate=(
                            SAMPLE_RATES[rate] if rate < len(SAMPLE_RATES) else None
                        ),
                    )
            if size > MAX_PAYLOAD_SIZE:
                raise ValueError(
                    f"Native chunk exceeds {MAX_PAYLOAD_SIZE} bytes: {size}"
                )
            end = offset + header_size + size
            if end > len(self.buffer):
                break
            if tag == 0xFD:
                self.video_meta = video_meta
            meta["length"] = size
            chunks.append((kind, bytes(self.buffer[offset + header_size : end]), meta))
            offset = end
        if offset:
            del self.buffer[:offset]
        return chunks


class XMMediaStream:
    """Bind a second TCP socket to the control session and receive native media."""

    def __init__(self, api: XMApiClient, *, native_stream_code: int | None = None):
        self.api = api
        self.native_stream_code = native_stream_code
        self.media_sock: socket.socket | None = None
        self.parser = XMChunkParser()
        self._chunks: Iterator[Chunk] | None = None
        self._channel = 0
        self._started = False
        self.on_wait: Callable[[], None] | None = None

    def validate_start(self, channel: int, stream: str) -> int:
        """Validate a monitor request and return its native stream code."""
        if not isinstance(channel, int) or not 0 <= channel <= 0xFFFFFFFF:
            raise ValueError("Channel must be a uint32")
        code = self.native_stream_code
        if code is None:
            if stream not in STREAM_CODES:
                raise ValueError(f"Unknown native stream: {stream}")
            code = STREAM_CODES[stream]
        if not isinstance(code, int) or not 0 <= code <= 0xFFFFFFFF:
            raise ValueError("Native stream code must be a uint32")
        return code

    def start(self, channel: int = 0, stream: str = "Main") -> None:
        if self.media_sock is not None:
            raise RuntimeError("The native media stream is already open")
        code = self.validate_start(channel, stream)
        if self.api.sock is None:
            raise RuntimeError("Log in before starting the native media stream")
        self._channel = channel
        self.media_sock = self.api.open_socket()
        self.parser = XMChunkParser()
        self._chunks = None
        try:
            claim = struct.pack("<8I", self.api.session, channel, 0, code, 0, 0, 0, 0)
            self.api.send_command(CLAIM, claim, sock=self.media_sock, sequence=0)
            start = struct.pack("<8I", self.api.session, channel, 0, 0, 0, 0, 0, 0)
            self.api.send_command(MONITOR, start)
        except BaseException:
            self.close()
            raise
        self._started = True
        logger.info(
            "[monitor] channel=%d stream=%s native_stream_code=%d",
            channel,
            stream,
            code,
        )

    def stop(self) -> None:
        if self._started:
            self._started = False
            stop = struct.pack("<8I", self.api.session, self._channel, 0, 1, 0, 0, 0, 0)
            self.api.send_command(MONITOR, stop)

    def close(self) -> None:
        if self.media_sock is not None:
            self.media_sock.close()
            self.media_sock = None
        self._started = False
        self._chunks = None

    def read_packet(self) -> tuple[int, bytes]:
        if self.media_sock is None:
            raise RuntimeError("The native media socket is not connected")
        packet = (
            read_packet(self.media_sock, on_wait=self.on_wait)
            if self.on_wait is not None
            else read_packet(self.media_sock)
        )
        if packet.session != self.api.session:
            raise RuntimeError("Session mismatch in native media packet")
        return packet.command, packet.payload

    def iter_chunks(self) -> Iterator[Chunk]:
        """Return one shared iterator so switching readers preserves sibling chunks."""
        if self._chunks is None:
            self._chunks = self._iter_chunks()
        return self._chunks

    def _iter_chunks(self) -> Iterator[Chunk]:
        while True:
            command, payload = self.read_packet()
            if command == MEDIA:
                yield from self.parser.feed(payload)
            else:
                logger.debug(
                    "[media socket] skipped command=%d bytes=%d", command, len(payload)
                )

    def read_first_chunk(self) -> Chunk:
        for chunk in self.iter_chunks():
            if chunk[0] in ("video", "audio"):
                return chunk
        raise ConnectionError("Native media stream ended")
