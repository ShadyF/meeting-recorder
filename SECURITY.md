# Security Policy

## Supported versions

This project is pre-1.0; only the latest release receives fixes.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private reporting:
[**Report a vulnerability**](https://github.com/ssKazal/meeting-recorder/security/advisories/new)

Please include what you did, what happened, and the impact. You can expect an
acknowledgement within a few days.

## Scope — why this matters here

This application can record your screen, microphone and system audio, so
please pay particular attention to:

- Anything that causes a recording to start **without the user's consent**
  (the permission prompt being bypassed or spoofed).
- Anything that discloses recordings, or writes them somewhere world-readable.
- Command injection via configuration values that reach `ffmpeg` (for example
  `capture_region`, `noise_model_path` or `output_dir`).
- Privilege issues in the `.deb` maintainer scripts.

## Design notes relevant to security

- The daemon runs as a **systemd user service** with your own privileges — never
  as root. It deliberately is not a system service.
- Recordings are written to `~/Videos/MeetingRecorder/` (configurable) and owned
  by you.
- Recordings are uploaded only after the user explicitly runs
  `meeting-recorder speakr upload PATH`. There is no automatic upload. The
  public `speakr_url` may be configured, but the bearer token is accepted only
  from `MEETING_RECORDER_SPEAKR_TOKEN`; it is never stored in configuration,
  CLI arguments, or SQLite.
- Speakr's local publication database stores only bounded public state under
  `XDG_STATE_HOME` (or the user's local state directory); it contains no
  credentials or private Meeting metadata.
- Detection reads audio-stream metadata via `pactl`; it never reads audio content
  to decide whether to record.
