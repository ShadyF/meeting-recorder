# Speakr v0.10.5-alpha tag API contract

## Scope

This note verifies the tag API used by Meeting Recorder against the official
Speakr [`v0.10.5-alpha`](https://github.com/murtaza-nasir/speakr/releases/tag/v0.10.5-alpha)
release, commit `7af15d67040b8bc8d1079e4870f4548d2295c334`.

There is no official `v0.10.5` tag or release. The exact Git ref and release API
both return 404. The supported target is therefore `v0.10.5-alpha`.

The release notes describe migration hardening only. Comparing
[`v0.10.3-alpha...v0.10.5-alpha`](https://github.com/murtaza-nasir/speakr/compare/v0.10.3-alpha...v0.10.5-alpha)
shows no tag-route or upload-contract changes.

## Tag discovery

Authenticated `GET /api/v1/tags` returns an object with a `tags` array. Each tag
contains an integer `id`, a display `name`, and group metadata. The response
contains the caller's personal tags followed by tags from groups in which the
caller is a member. Each section is ordered by name.

The endpoint has no pagination inputs or response metadata, and the
implementation has no tag-count limit. General API documentation limits GET
requests to 100 per minute.

Sources:

- [API reference](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/docs/user-guide/api-reference.md)
- [Tag route implementation](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/src/api/tags.py)

## Identifier stability and access

Tag IDs are integer database identifiers and are the values accepted by upload.
Renaming a tag updates the existing row, so its ID remains stable. Deleting a tag
removes its recording associations and invalidates cached IDs.

Personal-tag mutations require ownership. Group-tag mutations require group
administrator access. Discovery includes only tags accessible to the caller.

Source: [tag implementation](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/src/api/tags.py).

## Upload-time attachment

`POST /api/v1/recordings/upload` accepts contiguous indexed multipart fields:

```text
tag_ids[0]=1
tag_ids[1]=2
```

Parsing stops at the first missing index. Meeting Recorder must therefore number
fields contiguously from zero. The legacy singular `tag_id` field is also
accepted upstream but is not needed. For zero tags, omit all tag fields.

Valid tag associations are inserted and committed before transcription is
queued and before the endpoint returns `202 Accepted`. Upload-time attachment is
the only tag mutation Meeting Recorder will use.

Sources:

- [API reference](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/docs/user-guide/api-reference.md)
- [API routes](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/src/api/api_v1.py)
- [Upload implementation](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/src/api/recordings.py)

## Invalid and inaccessible IDs

The upload implementation silently skips nonexistent or inaccessible tag IDs.
It does not reject the upload or report a per-tag error. Mixed input can
therefore partially apply: accessible IDs attach while invalid IDs disappear
without an error response.

Meeting Recorder cannot infer exact tag application from `202 Accepted` when it
uploads IDs that were not successfully prevalidated. To provide accurate status,
it must validate the frozen selection against a fresh accessible catalog before
upload or record the upload-time tag outcome as unknown.

Source: [upload implementation](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/src/api/recordings.py).

## Post-upload endpoint

Speakr documents `POST /api/v1/recordings/{id}/tags` with JSON shaped as
`{"tag_ids": [1, 2]}`. Its implementation handles IDs individually and can report
partial success.

Meeting Recorder's product contract forbids changing tags after a clip has been
accepted. It must not call this endpoint. Existing post-upload metadata updates
must omit tag IDs.

Sources:

- [API reference](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/docs/user-guide/api-reference.md)
- [API routes](https://raw.githubusercontent.com/murtaza-nasir/speakr/v0.10.5-alpha/src/api/api_v1.py)

## Errors and integration boundary

The API documents JSON errors shaped as `{"error": "..."}` and common `400`,
`401`, `403`, and `404` responses. Bearer authentication remains the supported
Meeting Recorder transport. Tokens are account credentials and must never be
stored in tag caches, sidecars, publication rows, diagnostics, or logs.

For this pinned release, the safe integration is:

1. Fetch the caller's accessible tags with Bearer authentication.
2. Cache and persist integer IDs plus display names, using IDs as authority.
3. Revalidate frozen IDs before upload when an exact applied set is required.
4. Send only contiguous `tag_ids[n]` multipart fields during the initial upload.
5. Omit tag fields for no tags.
6. Never mutate remote tags after `202 Accepted`.

An upgrade to another Speakr release requires revalidation of these assumptions.
