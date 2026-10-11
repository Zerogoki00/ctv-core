"""Native DVRIP control connection, authentication and keepalive."""

import logging
import math
import socket
import struct
from dataclasses import dataclass

from .protocol import (
    KEEPALIVE,
    LOGIN,
    SUCCESS,
    encode_packet,
    fixed_c_string,
    read_packet,
    sofia_hash,
)

logger = logging.getLogger(__name__)


class AuthenticationError(RuntimeError):
    """A rejected native login must not trigger automatic retries."""


@dataclass(frozen=True)
class NativeReply:
    command: int
    session: int
    return_code: int
    words: tuple[int, ...]


class XMApiClient:
    """Own one authenticated native control socket and its session counter."""

    def __init__(
        self, host: str, port: int, user: str, password: str, *, timeout: float = 5.0
    ):
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Socket timeout must be finite and positive")
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.session = 0
        self.sequence = 0
        self.alive_interval = 20.0

    def open_socket(self) -> socket.socket:
        return socket.create_connection((self.host, self.port), timeout=self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send_command(
        self,
        command: int,
        payload: bytes = b"",
        *,
        sock: socket.socket | None = None,
        sequence: int | None = None,
        flag1: int = 0,
    ) -> NativeReply:
        connection = sock if sock is not None else self.sock
        if connection is None:
            raise RuntimeError("The DVRIP control socket is not connected")
        if sequence is None:
            sequence = self.sequence
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        connection.sendall(
            encode_packet(
                command,
                payload,
                self.session,
                sequence,
                flag1=flag1,
            )
        )
        packet = read_packet(connection)
        if packet.command != command + 1:
            raise RuntimeError(
                f"Expected reply {command + 1}, received {packet.command}"
            )
        minimum_words = 6 if command == LOGIN else 4
        if len(packet.payload) % 4 or len(packet.payload) < minimum_words * 4:
            raise ValueError(
                f"Malformed native reply {packet.command}: {len(packet.payload)} bytes"
            )
        words = struct.unpack(f"<{len(packet.payload) // 4}I", packet.payload)
        return_code = words[2] if command == LOGIN else words[1]
        if return_code != SUCCESS:
            if command == LOGIN:
                raise AuthenticationError(
                    f"DVR login rejected: return code {return_code}"
                )
            raise RuntimeError(
                f"DVRIP command {command} rejected: return code {return_code}"
            )
        session = words[4] if command == LOGIN else words[0]
        if session != packet.session or (command != LOGIN and session != self.session):
            raise RuntimeError(f"Session mismatch in native reply {packet.command}")
        logger.debug(
            "[reply] command=%d session=0x%08X sequence=%d words=%s",
            packet.command,
            session,
            packet.sequence,
            words,
        )
        return NativeReply(packet.command, session, return_code, words)

    def login(self) -> None:
        if self.sock is not None:
            raise RuntimeError("The DVRIP client is already connected")
        payload = fixed_c_string(self.user, 32)
        payload += fixed_c_string(sofia_hash(self.password), 32)
        payload += struct.pack("<II", 1, 2)
        self.session = self.sequence = 0
        self.sock = self.open_socket()
        try:
            reply = self.send_command(LOGIN, payload, flag1=1)
        except BaseException:
            self.close()
            raise
        self.session = reply.session
        self.alive_interval = float(reply.words[1] or 20)
        self.sequence = 0
        logger.info(
            "[login] session=0x%08X advertised_keepalive=%.0fs",
            self.session,
            self.alive_interval,
        )

    def keepalive(self) -> None:
        # The captured native client uses an empty body and sequence zero.
        self.send_command(KEEPALIVE, sequence=0)
