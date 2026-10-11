# Contributing

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

FFmpeg, FFprobe, and mpv are optional for development. Installing them enables
the media integration tests. On Debian or Ubuntu:

```bash
sudo apt install ffmpeg mpv
```

## Checks

```bash
python -m black src tests
python -m black --check src tests
python -m unittest discover -s tests -v
python -m build
```

Tests use local emulators, subprocess pipes, and generated H.264/G.711 data.
No camera credentials or live device are needed. Protocol tests require local
socket communication; restricted environments may prevent those tests from running.

Keep code, comments, and documentation in English. Use comments for protocol
constraints and non-obvious behavior. Add tests for changes to protocol handling,
resource ownership, or media output.

## Release preparation

Update the version in `pyproject.toml` and the release notes in `CHANGELOG.md`.
Run the checks above, then test a wheel installation in a fresh environment:

```bash
python3 -m venv /tmp/ctv-core-release
/tmp/ctv-core-release/bin/python -m pip install dist/*.whl
/tmp/ctv-core-release/bin/ctv-play --help
/tmp/ctv-core-release/bin/python -m ctv_core --help
```

GitHub Actions builds the wheel and source archive and saves them as workflow
artifacts. They can be attached to a GitHub release.
