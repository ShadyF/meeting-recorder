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
- Speakr publication follows `speakr_publication_mode`: `disabled` creates no
  automatic jobs or daemon attempts, `manual` retries existing explicit jobs,
  and `automatic` creates automatic publication jobs only for newly completed
  Recordings from the daemon or `meeting-recorder record`. Explicit
  `meeting-recorder speakr upload ...` commands remain available in every mode.
  The daemon never scans historical directories to create publication jobs.
- Normal network attempts (`upload PATH`, `upload --all`, and `upload --retry
  JOB`) require NetworkManager to report an active Wi-Fi SSID that exactly
  matches `speakr_allowed_ssids`: entries are case-sensitive raw UTF-8 strings,
  at most 32 UTF-8 bytes, with no trimming or normalization. Unknown,
  unavailable, or nonmatching Wi-Fi waits without contacting Speakr. `--force`
  is accepted only for those network forms and bypasses only the SSID gate;
  HTTPS, authentication, file identity, lease, reconciliation, and other state
  and file-safety checks still apply. An SSID match is not authentication.
- Production `speakr_url` must use HTTPS. The bearer token is accepted only from
  `MEETING_RECORDER_SPEAKR_TOKEN`; it is never stored in configuration, CLI
  arguments, or SQLite.
- Speakr's local publication database stores public-only durable progress fields
  plus one protected private filesystem locator for the current Recording path
  as operational state. The locator is kept inside the protected 0700 state
  directory and 0600 SQLite boundary under `XDG_STATE_HOME` (or the user's
  local state directory); the database contains no credentials or copied
  private Meeting title, notes, or participants.
- Detection reads audio-stream metadata via `pactl`; it never reads audio content
  to decide whether to record.

### Explicit local publication cleanup

`meeting-recorder cleanup --older-than DAYS` is a preview. `DAYS` must be at
least `1`; only `--delete` performs irreversible local deletion. Cleanup is
CLI-only and is not started by the daemon, publication automation, a token or
SSID check, D-Bus, HTTP, or any other publication path.

Deletion is limited to an old Recording path whose complete same-path group has
only `published`, unleased jobs with one matching Recording SHA-256. Cleanup
hashes the media again and checks the exact adjacent
`<media filename>.meeting.json` sidecar when one exists. It fails closed for
unsafe or malformed entries, identity or hash mismatches, symlinks, hardlinks,
out-of-root paths, and changes detected during the operation, stopping with an
incomplete result rather than trusting those entries. It removes only the media
and that exact sidecar, not unrelated neighboring files.

The destructive path first records a durable intent, uses private quarantine
names, and fsyncs namespace changes before advancing its journal phase. A crash
can leave an intent or quarantine entry; only a later explicit `--delete` may
resume it, after the recorded group and file identities still validate. There
is no Trash or user recovery mechanism. A completed cleanup retains each
publication job as `local_removed` with its publication history, while removing
the local path from the job.
