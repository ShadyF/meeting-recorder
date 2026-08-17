# Contributing

Thanks for taking the time to contribute! This is a small, dependency-light project — it should
take about five minutes to get running.

## Quick start

```bash
git clone https://github.com/ssKazal/meeting-recorder.git
cd meeting-recorder

sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-notify-0.7 \
                 gir1.2-appindicator3-0.1 ffmpeg pulseaudio-utils \
                 x11-utils x11-xserver-utils

python3 -m meeting_recorder run -v      # run the detector from source
python3 tests/run_tests.py              # run the tests
```

There is **nothing to `pip install`**. The app runs on the system `python3` with the distro's
PyGObject, and the tests use a tiny zero-dependency runner — no pytest, no virtualenv.

> **You need an X11 session** to exercise recording (`echo $XDG_SESSION_TYPE` must print `x11`).
> The unit tests themselves run fine headless.

If you have the package installed, stop it first so two daemons don't fight over the same
notification and tray: `meeting-recorder stop`.

## Devcontainer baseline

Open this repository in a Development Container with your editor's **Reopen in Container** command.
The Ubuntu 24.04 container runs as the non-root `developer` user and supports only the headless
checks below:

```bash
python3 tests/run_tests.py
python3 -m compileall -q meeting_recorder
```

It deliberately has no desktop integration, capture runtime, host sockets, or credentials.
`.deb` build and support are outside this development baseline; inherited Debian packaging files
remain available for their separate workflows.

## Tests

```bash
python3 tests/run_tests.py     # all tests, ~1 second
```

Every test must pass before a PR is merged; CI runs the same command.

The test surface is deliberately the **pure functions** — command construction, stream parsing,
allowlist matching, debounce logic, geometry. Anything touching ffmpeg, GTK or D-Bus lives at the
edges and is exercised by hand. If you add logic, add it as a pure function so it can be tested.

## Architecture — read this before changing the recorder

[`CLAUDE.md`](CLAUDE.md) documents the design and, importantly, **why** it is the way it is. The
short version:

- **Detection** = a known app holding a microphone capture stream (`pactl`), with a debounce.
  Monitor-only streams (music players) are ignored on purpose.
- **Recording is two-stage.** Live capture writes video + *unprocessed* mic/system tracks; a
  finalize pass then denoises, loudness-normalizes each source, mixes, and stream-copies the video.

Two hard-won rules, both with a real bug behind them:

1. **Never put audio filters in the live capture path.** `loudnorm` buffers ~3s, which silently
   truncated the last ~3 seconds of every recording and made a clean pause boundary impossible.
2. **Pause must end a segment, not freeze the process.** `SIGSTOP` looks like it works but the input
   queues keep buffering, so the paused span gets encoded on resume.

Keep `-tune zerolatency` and `-thread_queue_size` on the capture command — without them a busy
encoder starves `x11grab` and the first few seconds are a frozen black screen.

## Pull requests

1. Branch from `main`.
2. Keep the change focused; match the surrounding style (type hints, short docstrings, comments that
   explain *why* rather than *what*).
3. Run `python3 tests/run_tests.py`.
4. If you changed recording or detection behaviour, **verify it for real** — record a short clip and
   check the duration and audio, don't rely on tests alone.
5. Update `README.md` / `CHANGELOG.md` (under an `Unreleased` heading) if behaviour changed.

## Reporting bugs

Please use the issue templates — they ask for `XDG_SESSION_TYPE`, your distro and the service log,
which is almost always what's needed. The single most common report is **"black screen / nothing
records" on a Wayland session**, which this release does not support yet.

Useful diagnostics to include:

```bash
meeting-recorder status              # service state + detected capture streams
journalctl --user -u meeting-recorder -n 50 --no-pager
echo $XDG_SESSION_TYPE               # must be x11
```

## Good first issues

- **Wayland support** (the big one) — screen capture via the PipeWire portal instead of `x11grab`.
- Follow a window when it moves during window-capture mode.
- Show finalize progress in the notification.
- More apps in the default allowlist.

## Releasing (maintainers)

Releases are automated. Bump the version, then tag:

```bash
# 1. bump __version__ in meeting_recorder/__init__.py and version in pyproject.toml,
#    the README badge/filename, the man page .TH line, and add a CHANGELOG section
git commit -am "Release vX.Y.Z"
git tag -a vX.Y.Z -m "vX.Y.Z"
git push --follow-tags
```

Pushing the tag triggers [`.github/workflows/release.yml`](.github/workflows/release.yml), which
builds the `.deb`, extracts the matching `CHANGELOG.md` section as the release notes, and publishes
the GitHub Release with the package attached.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
