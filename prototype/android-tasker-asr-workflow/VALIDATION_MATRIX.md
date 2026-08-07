# Validation matrix

**Prototype only; no secrets in evidence.** Record screen captures, exported project metadata, upload IDs, or queue state in the notes column. A successful upload must be checked against the Speakr ASR integration contract, including `idempotent_replay` for a duplicate replay.

## Pixel 7

| Metadata | Value |
| --- | --- |
| Device | Pixel 7 |
| Android build | Android 17 `CP2A.260705.006` |
| Tasker | 6.6.20 |
| ASR | Latest installed `com.nll.asr` |
| Project export | Candidate: `Meeting_ASR_Pixel_7.prj.xml`; import pending |

| Test | Status | Evidence / notes |
| --- | --- | --- |
| Project import | Not run | |
| Calendar strong signal | Not run | |
| App alone rejection | Not run | |
| Notification alone rejection | Not run | |
| App + notification within 10 min | Not run | |
| Cooldown duplicate suppression | Not run | |
| Manual re-arm | Not run | |
| ASR launch | Not run | |
| Direct/auto start vs tap | Not run | Record whether launch starts recording or needs a user tap. |
| Continued background recording | Not run | |
| Stop behavior | Not run | Confirm manual ASR stop; silent Tasker stop is not assumed. |
| Away-from-home Wi-Fi queue | Not run | |
| Home Wi-Fi arrival upload | Not run | |
| Failed retry | Not run | Verify 1–2 hour retry or manual queue upload. |
| Duplicate replay exactly once | Not run | Verify server response/record ID shows one idempotent replay. |

## Galaxy Tab S9

| Metadata | Value |
| --- | --- |
| Device | Galaxy Tab S9 (pending) |
| Android build | Pending |
| Tasker | Pending |
| ASR | Pending |
| Project export | Pending |

| Test | Status | Evidence / notes |
| --- | --- | --- |
| Project import | Not run | |
| Calendar strong signal | Not run | |
| App alone rejection | Not run | |
| Notification alone rejection | Not run | |
| App + notification within 10 min | Not run | |
| Cooldown duplicate suppression | Not run | |
| Manual re-arm | Not run | |
| ASR launch | Not run | |
| Direct/auto start vs tap | Not run | Record whether launch starts recording or needs a user tap. |
| Continued background recording | Not run | |
| Stop behavior | Not run | Confirm manual ASR stop; silent Tasker stop is not assumed. |
| Away-from-home Wi-Fi queue | Not run | |
| Home Wi-Fi arrival upload | Not run | |
| Failed retry | Not run | Verify 1–2 hour retry or manual queue upload. |
| Duplicate replay exactly once | Not run | Verify server response/record ID shows one idempotent replay. |

## Unresolved limitations

- `Meeting_ASR_Pixel_7.prj.xml` is an unverified direct-import candidate; a successful Pixel import and re-export as `Meeting_ASR_Pixel_7_verified.prj.xml` is still required.
- ASR does not provide a documented public Tasker start/stop intent for this workflow. Android background-start policy can require the `Open ASR` tap.
- Notification wording and `New Only` behavior vary by app and Tasker/Android version; test the individual owner-app profiles.
- Samsung battery management may affect profile delivery, ASR launch, recording, and queued upload behavior; the Galaxy result cannot be inferred from the Pixel.
