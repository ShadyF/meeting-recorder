# Speakr tag API contract

## Scope and evidence level

This note answers issue [#44](https://github.com/ShadyF/meeting-recorder/issues/44) for the pinned upstream release [`v0.10.3-alpha`](https://github.com/murtaza-nasir/speakr/tree/v0.10.3-alpha). **Documented** below means that the release's API reference states the behavior; it is the closest available API contract. **Observed** means that the release's server implementation exhibits the behavior, but the API reference does not promise it. Treat observed behavior as compatibility information, not a durable integration guarantee.

The deployed Speakr version remains an external prerequisite: this contract must be rechecked against the exact deployed release before implementation or upgrade. Multi-tag support was introduced in [`v0.5.0-alpha`](https://github.com/murtaza-nasir/speakr/releases/tag/v0.5.0-alpha), and the upload API was extended in [`v0.8.15-alpha`](https://github.com/murtaza-nasir/speakr/releases/tag/v0.8.15-alpha); neither release note makes an unpinned deployment equivalent to `v0.10.3-alpha`.

## Supported tag discovery

- **Documented:** `GET /api/v1/tags` returns the caller's personal tags and the group tags to which the caller has access. The response is an object with a `tags` array; each tag has an integer `id`, which is the value used by the attachment APIs. The reference describes no `page`, `per_page`, cursor, or pagination response field for this endpoint. [API reference: List Tags](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/docs/user-guide/api-reference.md#list-tags)
- **Observed:** the tag-list implementation builds the accessible personal and group set and returns it in one response; it does not parse pagination inputs. Therefore Meeting Recorder can make one list request for this release, but must not infer that the list is permanently unpaginated. [Tag route implementation](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/src/api/tags.py)
- **Decision:** cache the displayed tag metadata with its integer `id`, but select and send IDs rather than names. Refresh the list when connectivity permits and revalidate selected IDs when publishing; names, colors, access, and existence are not stable identifiers.

## Authentication and secret handling

- **Documented:** API endpoints require authentication. Speakr recommends `Authorization: Bearer <token>`; `X-API-Token`, `API-Token`, and a `token` query parameter are alternatives. [API reference: Authentication](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/docs/user-guide/api-reference.md#authentication) [API tokens: Using Your Token](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/docs/user-guide/api-tokens.md#using-your-token)
- **Documented:** a personal token acts with the same access as its owner and should be treated as a password/full-account credential. [API tokens: Overview and security notice](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/docs/user-guide/api-tokens.md#overview)
- **Decision:** Meeting Recorder uses only the Bearer header and protects the token as an account secret. It does not use query-string token transport, which can expose a credential in URLs and logs.

## Attach tags while uploading

- **Documented:** `POST /api/v1/recordings/upload` is multipart form data. It accepts `tag_ids[0]`, `tag_ids[1]`, and further indexed fields for multiple integer tag IDs; `tag_id` is a legacy single-tag field. A successful upload returns `202 Accepted` and queues transcription. [API reference: Upload Recording](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/docs/user-guide/api-reference.md#upload-recording)
- **Observed:** the v0.10.3-alpha upload handler reads indexed `tag_ids[...]` fields as well as the legacy `tag_id`, associates the resolved tags before the upload path queues work, and exposes no dedicated zero-tag value. [Upload endpoint implementation](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/src/api/api_v1.py)
- **Decision:** send one multipart field per selected tag, such as `tag_ids[0]=1` and `tag_ids[1]=2`; omit both `tag_ids[...]` and `tag_id` when none are selected. Treat `202` as acceptance and queuing, not completion. Use upload-time attachment whenever tag-driven settings are required for the first processing attempt.

## Attach tags after upload

- **Documented:** `POST /api/v1/recordings/{id}/tags` accepts JSON in the form `{"tag_ids": [1, 2]}` to add tags to a recording. [API reference: Add Tags to Recording](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/docs/user-guide/api-reference.md#add-tags-to-recording)
- **Observed:** the handler also accepts a legacy singular `tag_id`; it skips a link that already exists, attempts the requested tags individually, and reports failures per tag. Consequently a request can leave a recording with a successful subset of its requested tags. This is implementation behavior, not an explicitly documented atomicity or response contract. [Tag-attachment implementation](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/src/api/api_v1.py)
- **Decision:** use only the documented `tag_ids` array. Treat a post-upload call as a reconciliation operation: inspect its result, retain/report per-tag failures, and do not model it as all-or-nothing or depend on duplicate links causing an error.

## Failures, mutation, and timing boundaries

- **Documented:** error responses use JSON shaped as `{"error": "..."}`. The reference identifies `400` for invalid parameters, `401` for a missing or invalid token, `403` for insufficient permission, and `404` for a missing resource. [API reference: Error Responses](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/docs/user-guide/api-reference.md#error-responses)
- **Observed:** the tag routes update a tag object in place when renamed, so its integer ID is retained; deletion deletes the tag and its recording associations. A cached ID can therefore remain the same through a rename but becomes invalid after deletion. [Tag update and deletion implementation](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/src/api/tags.py)
- **Observed:** upload-time tag association occurs before work is queued. The separate post-upload endpoint proves that later attachment is supported, but neither the API reference nor these handlers guarantees that tag-derived transcription settings will affect work that is already queued or running. [Upload and tag-attachment implementation](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/src/api/api_v1.py)
- **Decision:** distinguish permanent input/access failures (`400`, `401`, `403`, `404`) from an accepted upload; refresh and ask the operator to resolve deleted or inaccessible selected tags rather than trusting cache indefinitely. Do not promise that a later tag attachment changes transcription settings for an already-queued recording.

## Implementation boundary for the map

For Speakr `v0.10.3-alpha`, the safe integration contract is: list accessible tags with Bearer authentication; persist/select their integer IDs; attach selected IDs in multipart upload fields before accepting the `202` queue response; omit fields for no selection; and use the documented JSON array endpoint only to reconcile a later attachment, handling partial outcomes. This note does not specify Meeting Recorder's cache, persistence, retry, or user-interface policy beyond the API constraints above.
