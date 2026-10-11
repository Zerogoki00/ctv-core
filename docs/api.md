# API reference

## CTVClient

`CTVClient` owns one control connection and one media connection. All camera I/O
runs in the caller's thread; each client supports one live stream and one reader.

```python
from ctv_core import CTVClient

camera = CTVClient(
    "192.0.2.10",
    user="admin",
    password="",
    port=34567,
    timeout=5.0,
    channel=0,
    stream="Main",
)
```

Use a context manager or call `open()` and `close()` explicitly. `open()` performs
Login → Claim → Start. `close()` attempts Stop and releases both sockets; repeated
calls are safe. `native_stream_code` overrides the stream name's native code.

### Reading chunks

| Method | Result |
| --- | --- |
| `read_chunk(stop=None)` | One complete chunk; cancellation raises `StreamStopped` |
| `iter_chunks(stop=None)` | Video, audio, and info chunks in arrival order |
| `iter_video_chunks(stop=None)` | Video chunks only; audio and info are consumed |
| `poll(stop=None)` | Check cancellation and send keepalive when due |

A chunk is `(kind, data, metadata)`, where `kind` is `"video"`, `"audio"`, or `"info"`,
`data` is `bytes`, and `metadata` is a dictionary. Native headers are removed. Chunks
split across packets are reassembled, and multiple chunks in a packet are retained.
Switching between the read methods preserves buffered chunks.

Video payloads are compressed H.264 Annex B bytes, not decoded images or MP4 files.
Keyframes include SPS/PPS. A stream may begin with a P-frame whose metadata is
incomplete; wait for an I-frame before starting a decoder.

### Metadata

| Kind | Fields |
| --- | --- |
| All | `tag`, `length` |
| Video | `frame` (`I` or `P`), `video_codec`, `video_codec_id`, `fps`, `width`, `height` |
| I-frame | `timestamp_raw`, `datetime` |
| Audio | `audio_codec`, `audio_codec_id`, `sample_rate`, `sample_rate_code` |

DVR dates represent local calendar time without a time zone. P-frames have no
separate timestamp. Applications generating containers must calculate PTS/DTS.
Unknown codecs retain their original payloads and are labeled `unknown-video-N`
or `unknown-audio-N`; the parser does not decode them.

### Cancellation

Pass a `threading.Event` to request cancellation from another thread:

```python
from threading import Event

from ctv_core import CTVClient

stop = Event()

with CTVClient("192.0.2.10", "admin", "") as camera:
    for kind, data, metadata in camera.iter_chunks(stop=stop):
        print(kind, len(data))
```

Calling `stop.set()` ends an iterator normally. `read_chunk()` instead raises
`StreamStopped`. Cancellation during media waits is checked about every 100 ms.
Control RPCs, including Stop during cleanup, remain bounded by the socket timeout.

### Keepalive and errors

Reading services keepalive, including while waiting for a partial packet. During
long processing pauses, call `poll()` in the same thread at least once every
`max(1, min(8, camera.api.alive_interval / 2))` seconds. To process chunks in another
thread, use a queue while keeping one owner of the camera client.

`AuthenticationError` indicates a rejected login. Socket errors, EOF, timeouts,
session mismatches, and malformed data propagate to the caller. A failed or
cancelled read closes both sockets. A new session can be started with `open()`;
the client does not reconnect automatically.

## Lower-level clients

`XMApiClient` owns the control session. `XMMediaStream` binds a second connection
and parses incoming media:

```python
from ctv_core import XMApiClient, XMMediaStream

api = XMApiClient("192.0.2.10", 34567, "admin", "", timeout=5.0)
media = XMMediaStream(api)

try:
    api.login()
    media.start(channel=0, stream="Main")
    kind, data, metadata = media.read_first_chunk()
    print(kind, len(data), metadata)
finally:
    try:
        media.stop()
    finally:
        media.close()
        api.close()
```

Keepalive, cancellation, and error cleanup are the caller's responsibility when
using the lower-level API. `media.on_wait` accepts a callback with no arguments
for servicing these tasks while waiting for packets.

`api.send_command(command, payload)` returns a
`NativeReply(command, session, return_code, words)`. Successful replies must use
`command + 1`, return code `100`, and the expected session. The caller supplies
the command body. `api.session`, `api.alive_interval`, `api.keepalive()`, and
`api.close()` are also available.

`media.read_packet()` returns `(command, payload)`. `media.iter_chunks()` and
`media.read_first_chunk()` share an iterator and preserve unread sibling chunks.

## Parsing saved payloads

Use `XMChunkParser` to parse media payloads from DVRIP command `1412`:

```python
from ctv_core import XMChunkParser

parser = XMChunkParser()
chunks = parser.feed(payload)
```

Reuse the same parser across payloads to retain incomplete headers and bodies.
The `ctv_core.protocol` module exposes `Packet`, `encode_packet`, `read_packet`,
`recv_exact`, `sofia_hash`, `fixed_c_string`, `decode_dvr_datetime`, and command
constants.

## FLV output

`ctv_core.flv.FLVWriter` combines H.264 and G.711 chunks into a timestamped FLV
stream. Its constructor takes a callable accepting `bytes`; call
`write_chunk(kind, data, metadata)` for each received chunk.

The writer waits for an IDR with SPS/PPS, discards earlier media, and preserves
compressed video and audio without transcoding. Audio sample counts provide the
audio clock; video advances by at least one frame period. It supports H.264 without
B-frames and mono G.711 A-law or mu-law at 8000 Hz. Changes to video frame rate,
dimensions, or audio format raise `ValueError`. FLV timestamps limit a single
stream to approximately 49.7 days.
