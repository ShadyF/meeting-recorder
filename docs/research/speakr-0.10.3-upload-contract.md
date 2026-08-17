# Speakr v0.10.3 upload contract

## Scope and release

**Facts**

- The canonical Speakr repository is [`murtaza-nasir/speakr`](https://github.com/murtaza-nasir/speakr).
- Release [`v0.10.3-alpha`](https://github.com/murtaza-nasir/speakr/releases/tag/v0.10.3-alpha) was published on 2026-08-07. The [release API record](https://api.github.com/repos/murtaza-nasir/speakr/releases/tags/v0.10.3-alpha) is the machine-readable release source.

**Planning implications**

- Treat this document as a contract for Speakr `v0.10.3-alpha`; revalidate it when upgrading Speakr.

## Standard recording upload

**Facts**

- `POST /api/v1/recordings/upload` accepts a multipart request with required `file`.
- Supported upload fields include `title`, `notes`, `meeting_date`, `file_last_modified`, `language`, speaker hints, prompt/model/folder/tag options, and `keep_audio_only`.
- A successful standard upload returns `202 Accepted` and queues processing.
- `participants` is not an upload field. Update participants with `PATCH /api/v1/recordings/{id}` after upload. `title` and `meeting_date` can be set during upload.
- Standard uploads detect duplicate content by SHA-256, but they are not replay-idempotent. An ambiguous timeout still requires duplicate detection and reconciliation by the client.

Sources: [`api_v1.py`](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.3-alpha/src/api/api_v1.py), [`recordings.py`](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.3-alpha/src/api/recordings.py), and the [API reference](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.3-alpha/docs/user-guide/api-reference.md).

**Planning implications**

- The desktop Speakr publisher should use this endpoint, provide `meeting_date` explicitly, apply participant names later with `PATCH`, and retain durable duplicate-reconciliation state for uncertain outcomes.

## Authentication

**Facts**

- Personal API tokens are full account credentials.
- Speakr recommends passing them in the `Authorization: Bearer <token>` header.

Source: [API tokens guide](https://raw.githubusercontent.com/murtaza-nasir/speakr/master/docs/user-guide/api-tokens.md).

**Planning implications**

- Store and transmit publisher tokens as account secrets; use the Bearer authorization header.

## Meeting-date resolution

**Facts**

- Speakr resolves a recording date in this order: explicit `meeting_date`; a filename date when the user has enabled parsing; `file_last_modified`; embedded media metadata; then current time.
- Filename parsing is controlled by the per-user `parse_filename_dates`, `filename_date_pattern`, and `filename_date_regex` settings. It is disabled by default.
- Auto parsing tries `yyyymmdd_hhmm`, `yyyy-mm-dd`, `yyyymmdd`, and `yymmdd_hhmm`. A bare `yymmdd` pattern is only accepted when explicitly configured.
- A custom regex must provide named `year`, `month`, and `day` groups; `hour` and `minute` are optional. Accepted years are 1970 through 2100.
- `client_tz_offset` follows JavaScript `getTimezoneOffset` semantics. It converts filename wall time to UTC. Without an offset, time-bearing values are treated as UTC; date-only values are anchored at noon UTC.

Sources: [`filename_dates.py`](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.3-alpha/src/utils/filename_dates.py) and its [tests](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.3-alpha/tests/test_filename_dates.py).

**Planning implications**

- The desktop publisher should send a timezone-aware `meeting_date` and use `file_last_modified` as its fallback. It should not depend on the disabled filename parser.

## ASR Voice Recorder integration upload

**Facts**

- `POST /api/v1/integrations/asr-voice-recorder/upload` accepts a secret token, `file`, and optional `file_name`, `date`, `note`, and `duration`; `duration` is ignored.
- It returns `200 OK`, including for the fileless test case.
- A retry within the replay window that matches user, content, filename, note, and date returns the original ID with `idempotent_replay`.
- This endpoint has no `title` or `participants` fields; exact metadata requires a later `PATCH`.

Source: [ASR Voice Recorder upload tests](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/tests/test_asr_voice_recorder_upload.py).

**Planning implications**

- Android NLL ASR should use a dedicated adapter for this endpoint so it can use its replay semantics without changing the desktop publisher contract.
