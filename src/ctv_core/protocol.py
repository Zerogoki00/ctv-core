"""Native DVRIP packet framing and password/date encoding."""

import hashlib
import select
import socket
import struct
import time
from dataclasses import dataclass
from datetime import datetime

HEADER = struct.Struct("<BBBBII2xHI")
MAX_PAYLOAD_SIZE = 16 * 1024 * 1024
LOGIN = 1000
KEEPALIVE = 1006
MONITOR = 1410
MEDIA = 1412
CLAIM = 1413
SUCCESS = 100
HASH_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class Packet:
    command: int
    session: int
    sequence: int
    payload: bytes
    version: int = 0


def sofia_hash(password: str) -> str:
    """Return the eight-character password equivalent used by native login."""
    digest = hashlib.md5(password.encode("utf-8")).digest()
    return "".join(
        HASH_ALPHABET[(a + b) % 62] for a, b in zip(digest[::2], digest[1::2])
    )


def fixed_c_string(value: str, size: int) -> bytes:
    raw = value.encode("utf-8")
    if b"\x00" in raw or len(raw) >= size:
        raise ValueError(
            f"Value must fit in a {size}-byte C string without embedded NULs"
        )
    return raw.ljust(size, b"\x00")


def encode_packet(
    command: int, payload: bytes, session: int, sequence: int, *, flag1: int = 0
) -> bytes:
    if len(payload) > MAX_PAYLOAD_SIZE:
        raise ValueError(f"DVRIP payload exceeds {MAX_PAYLOAD_SIZE} bytes")
    return (
        HEADER.pack(0xFF, 0, flag1, 0, session, sequence, command, len(payload))
        + payload
    )


def recv_exact(sock: socket.socket, size: int, *, on_wait=None) -> bytes:
    """Read exactly size bytes; a timeout or EOF is fatal to this connection."""
    data = bytearray()
    timeout = sock.gettimeout() if on_wait is not None else None
    deadline = time.monotonic() + timeout if timeout is not None else None
    while len(data) < size:
        if on_wait is not None:
            on_wait()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Native packet read exceeded its deadline")
            if not select.select([sock], [], [], 0.1)[0]:
                continue
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError(
                f"DVR closed the socket after {len(data)}/{size} bytes"
            )
        data.extend(chunk)
    return bytes(data)


def read_packet(sock: socket.socket, *, on_wait=None) -> Packet:
    magic, version, flag1, flag2, session, sequence, command, size = HEADER.unpack(
        recv_exact(sock, HEADER.size, on_wait=on_wait)
    )
    if magic != 0xFF:
        raise ValueError(f"Invalid DVRIP magic: 0x{magic:02x}")
    if size > MAX_PAYLOAD_SIZE:
        raise ValueError(f"DVRIP payload exceeds {MAX_PAYLOAD_SIZE} bytes: {size}")
    return Packet(
        command, session, sequence, recv_exact(sock, size, on_wait=on_wait), version
    )


def decode_dvr_datetime(value: int) -> datetime | None:
    """Decode the packed local calendar date; the wire carries no timezone."""
    try:
        return datetime(
            2000 + (value >> 26),
            (value >> 22) & 15,
            (value >> 17) & 31,
            (value >> 12) & 31,
            (value >> 6) & 63,
            value & 63,
        )
    except ValueError:
        return None
