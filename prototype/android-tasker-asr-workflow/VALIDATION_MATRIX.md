# Validation matrix

**Prototype only; no secrets in evidence.** Record screen captures, exported project metadata, upload IDs, or queue state in the notes column. A successful upload must be checked against the Speakr ASR integration contract, including `idempotent_replay` for a duplicate replay.

## Pixel 7

| Metadata | Value |
| --- | --- |
| Device | Pixel 7 |
| Android build | Android 17 `CP2A.260705.006` |
| Tasker | 6.6.20 |
| ASR | Latest installed `com.nll.asr` |
| Project export | Captured: `Meeting_ASR_Pixel_7_verified.prj.xml` normalized re-export; candidate retained as provenance |

| Test | Status | Evidence / notes |
| --- | --- | --- |
| Project import | Passed | Tasker 6.6.20 imported and re-exported all 6 profiles and 6 tasks. |
| Normalized re-export | Captured | `Meeting_ASR_Pixel_7_verified.prj.xml` from the Pixel 7 Tasker 6.6.20 re-export. |
| Calendar strong signal | Not run | |
| App alone rejection | Passed | Manual `Signal App` run produced no prompt. Current Webex package/class are `com.cisco.wx2.android` / `com.webex.teams.WebexLauncherActivity`. |
| Notification alone rejection | Passed | Manual `Signal Notification` run produced no prompt. |
| App + notification within 10 min | Passed | Manual app then notification signals produced one meeting prompt. |
| Cooldown duplicate suppression | Passed | Repeating paired signals without re-arming produced no new prompt. |
| Manual re-arm | Passed | Used between deterministic signal cases. |
| ASR launch | Passed | `Open ASR` launched the recorder; Tasker also warned that the Notify category was unset. |
| Direct/auto start vs tap | Passed | ASR opened and began recording without an additional start tap. |
| Continued background recording | Passed | ASR continued recording after Tasker waited briefly and automatically returned to the previous meeting app with Back. |
| Stop behavior | Passed | Leaving Meet triggered one automatic NLL stop after the 30-second end check; lifecycle returned to `IDLE` with `asr_active=0`, no error, and exactly one new recording file. |
| Away-from-home Wi-Fi queue | Passed | With home Wi-Fi disconnected, ASR retained the stopped recording in its upload queue instead of publishing it. |
| Home Wi-Fi arrival upload | Passed | Rejoining home Wi-Fi cleared the ASR queue and created exactly one new Speakr recording. |
| Failed retry | Not run | Deliberately skipped during Pixel validation; automatic/manual recovery from an attempted failed upload remains unverified. |
| Duplicate replay exactly once | Passed | Manually replaying the same ASR recording reused the existing Speakr recording instead of creating a duplicate. |

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

- The original `Meeting_ASR_Pixel_7.prj.xml` remains an unverified hand-assembled provenance candidate. `Meeting_ASR_Pixel_7_verified.prj.xml` was normalized by Tasker import/re-export, but behavior validation remains required.
- The final normalized export captures Webex foreground and notification contexts as `com.cisco.wx2.android` with `com.webex.teams.WebexLauncherActivity`; behavior validation remains required.
- ASR does not provide a documented public Tasker start/stop intent for this workflow. Android background-start policy can require the `Open ASR` tap.
- Notification wording and `New Only` behavior vary by app and Tasker/Android version; test the individual owner-app profiles.
- Samsung battery management may affect profile delivery, ASR launch, recording, and queued upload behavior; the Galaxy result cannot be inferred from the Pixel.
