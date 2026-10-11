"""Interleave native H.264/G.711 chunks as timestamped FLV tags."""

import re
import struct
from collections.abc import Callable

ANNEX_B_START = re.compile(b"\x00\x00(?:\x00)?\x01")
AUDIO_RATE = 8000


class FLVWriter:
    """Mux H.264 without B-frames and mono 8 kHz G.711 into a single pipe.

    Audio samples provide the audio clock. Video follows that position while
    advancing by at least one frame period. Data before the first IDR with
    SPS/PPS is discarded so both tracks share the playback origin.
    """

    def __init__(self, write: Callable[[bytes], None]):
        self.write = write
        self.video_format: tuple[int, int, int] | None = None
        self.configuration: bytes | None = None
        self.audio_codec: str | None = None
        self.audio_samples = 0
        self.video_next_ms = 0.0
        self.video_frames = 0
        self.audio_chunks = 0
        self.write(b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00")

    def _tag(self, kind: int, timestamp_ms: float, data: bytes) -> None:
        timestamp = int(round(timestamp_ms))
        if not 0 <= timestamp <= 0xFFFFFFFF:
            raise ValueError("Playback exceeded the FLV timestamp range (49 days)")
        if len(data) > 0xFFFFFF:
            raise ValueError("Frame exceeded the FLV tag size limit")
        header = bytes([kind]) + len(data).to_bytes(3, "big")
        header += (timestamp & 0xFFFFFF).to_bytes(3, "big")
        header += bytes([timestamp >> 24]) + b"\x00\x00\x00"
        self.write(header + data + struct.pack(">I", 11 + len(data)))

    def _metadata(self, fps: int, width: int, height: int) -> None:
        name = b"onMetaData"
        data = b"\x02" + struct.pack(">H", len(name)) + name
        fields = {"framerate": fps, "width": width, "height": height}
        data += b"\x08" + struct.pack(">I", len(fields))
        for key, value in fields.items():
            encoded = key.encode("ascii")
            data += struct.pack(">H", len(encoded)) + encoded
            data += b"\x00" + struct.pack(">d", value)
        self._tag(18, 0, data + b"\x00\x00\x09")

    @staticmethod
    def _avc_configuration(nals: list[bytes]) -> bytes | None:
        sps = [nal for nal in nals if nal[0] & 31 == 7]
        pps = [nal for nal in nals if nal[0] & 31 == 8]
        if not sps or not pps:
            return None
        if (
            len(sps[0]) < 4
            or len(sps) > 31
            or len(pps) > 255
            or any(len(nal) > 65535 for nal in sps + pps)
        ):
            raise ValueError("Invalid H.264 parameter sets")
        data = b"\x01" + sps[0][1:4] + b"\xff" + bytes([0xE0 | len(sps)])
        for nal in sps:
            data += struct.pack(">H", len(nal)) + nal
        data += bytes([len(pps)])
        for nal in pps:
            data += struct.pack(">H", len(nal)) + nal
        return data

    def _video(self, data: bytes, meta: dict) -> None:
        if self.configuration is None and meta.get("frame") == "P":
            return
        if meta.get("video_codec") != "h264":
            raise ValueError(f"Unsupported video codec: {meta.get('video_codec')}")
        fps = meta.get("fps", 0)
        if not 0 < fps <= 120:
            raise ValueError(f"Invalid video frame rate: {fps}")
        video_format = (fps, meta["width"], meta["height"])
        if self.video_format is not None and video_format != self.video_format:
            raise ValueError("Video frame rate or dimensions changed during playback")
        if not ANNEX_B_START.match(data):
            raise ValueError("Expected H.264 Annex B data")
        nals = [nal for nal in ANNEX_B_START.split(data) if nal]
        keyframe = any(nal[0] & 31 == 5 for nal in nals)
        configuration = self._avc_configuration(nals)
        if self.configuration is None and (not keyframe or configuration is None):
            return
        if self.video_format is None:
            self._metadata(*video_format)
            self.video_format = video_format
        timestamp = max(self.video_next_ms, self.audio_samples * 1000 / AUDIO_RATE)
        if configuration is not None and configuration != self.configuration:
            self._tag(9, timestamp, b"\x17\x00\x00\x00\x00" + configuration)
            self.configuration = configuration
        # AVC packet type 1, with zero composition offset (PTS equals DTS).
        frame = bytes([0x17 if keyframe else 0x27, 1, 0, 0, 0])
        frame += b"".join(struct.pack(">I", len(nal)) + nal for nal in nals)
        self._tag(9, timestamp, frame)
        self.video_next_ms = timestamp + 1000 / fps
        self.video_frames += 1

    def _audio(self, data: bytes, meta: dict) -> None:
        if self.configuration is None:
            return
        codec = meta.get("audio_codec")
        if codec not in ("g711a", "g711u") or meta.get("sample_rate") != AUDIO_RATE:
            raise ValueError(f"Unsupported audio format: {meta}")
        if self.audio_codec is not None and codec != self.audio_codec:
            raise ValueError("Audio codec changed during playback")
        self.audio_codec = codec
        # FLV sound formats 7/8 imply mono 8 kHz G.711, independent of rate bits.
        flags = 0x72 if codec == "g711a" else 0x82
        self._tag(8, self.audio_samples * 1000 / AUDIO_RATE, bytes([flags]) + data)
        self.audio_samples += len(data)
        self.audio_chunks += 1

    def write_chunk(self, kind: str, data: bytes, meta: dict) -> None:
        if not data or kind == "info":
            return
        if kind == "video":
            self._video(data, meta)
        elif kind == "audio":
            self._audio(data, meta)
        else:
            raise ValueError(f"Unsupported media kind: {kind}")
