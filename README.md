# ctv-core

A Python client for native XM/DVRIP cameras and DVRs, with live video and audio
playback through mpv.

The library handles authentication, control commands, keepalive, stream lifecycle,
and incremental media parsing. The `ctv-play` command sends H.264 video and G.711
audio to mpv through a single FLV pipe, without transcoding or temporary media files.

## Requirements

- Python 3.10 or newer.
- A device supporting the native binary XM/DVRIP protocol.
- mpv installed and available on `PATH` for playback.

The library has no third-party Python dependencies. An external FFmpeg process is
not required for playback.

## Installation

From a checkout of this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` instead.

## Live playback

```bash
ctv-play
```

The application prompts for the camera IP address, login, and password. The default
login is `admin`. Password input is hidden and is not saved to disk.

Connection options can also be supplied on the command line:

```bash
ctv-play --host 192.0.2.10 --user admin --channel 0 --stream Main
```

The password is still requested interactively. Channels are numbered from zero.

| Option | Default | Description |
| --- | --- | --- |
| `--host` | Prompt | Camera IPv4 or IPv6 address |
| `--user` | Prompt, then `admin` | Camera login |
| `--port` | `34567` | Native DVRIP port |
| `--channel` | `0` | Camera channel |
| `--stream` | `Main` | `Main`, `Extra1`, or `Extra2` |
| `--timeout` | `5` | Socket timeout in seconds |
| `--mpv` | `mpv` | Player executable name or path |

Close the mpv window, press `q` in the player, or press Ctrl+C in the terminal to
stop playback and close the camera session. Keepalive continues while the player's
input pipe is full. Pausing can increase the delay behind the live stream.

The command is also available as `python -m ctv_core` or
`python -m ctv_core.player`. For an uninstalled checkout:

```bash
PYTHONPATH=src python3 -m ctv_core
```

## Library usage

```python
from getpass import getpass

from ctv_core import CTVClient

with CTVClient("192.0.2.10", "admin", getpass("Camera password: ")) as camera:
    for kind, data, metadata in camera.iter_chunks():
        print(kind, len(data), metadata)
```

Each chunk is a `(kind, data, metadata)` tuple. Video bytes contain compressed
H.264 Annex B data, and audio bytes contain the original compressed samples. Use
`iter_video_chunks()` when only video is needed.

The context manager logs in, claims the media connection, starts the stream, and
attempts to stop it on exit. Both connections are released on errors or cancellation.
See the [API reference](docs/api.md) for cancellation, metadata, keepalive, and
lower-level access.

## Supported streams

The verified profile is channel `0`, `Main` (native stream code `2`), H.264
1920×1080 at 25 fps, and mono G.711 A-law at 8000 Hz. `Extra1` (code `3`) and
`Extra2` (code `4`) are experimental.

Playback supports H.264 without B-frames and mono G.711 A-law or mu-law at 8000 Hz.
Audio must be enabled on the device. Archive playback, H.265 decoding, automatic
reconnection, and general device configuration are not implemented.

## Development

```bash
python -m pip install -e '.[dev]'
python -m black --check src tests
python -m unittest discover -s tests -v
python -m build
```

Tests use local protocol emulators and synthetic media; a camera is not required.
FFmpeg, FFprobe, and mpv enable additional media integration tests. These tests are
skipped when the required tools are unavailable.

GitHub Actions checks Python 3.10–3.14, code formatting, and distribution builds.
Build output is written to `dist/`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
development workflow and [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

[MIT](LICENSE).
