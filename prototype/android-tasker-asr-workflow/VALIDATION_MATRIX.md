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
| Device | Galaxy Tab S9 |
| Android build | Android 16 `BP4A.251205.006.X710XXS6EZG3` |
| Tasker | 6.6.20 |
| ASR | Latest installed `com.nll.asr` |
| Project export | `MR_Meet_Lifecycle.prj.xml` plus normalized Galaxy standalone exports: `MR Inspect Meetings`, `Launch ASR`, `MR Stop ASR`, and `MR Meet Posted Wake` |

| Test | Status | Evidence / notes |
| --- | --- | --- |
| Project import | Passed | Lifecycle project imported and reset successfully. |
| Meet notification lifecycle | Passed | Galaxy signature is Meet package, empty category, persistent `true`, button 1 `Hang Up`; `%anqueryok` is unset. The single Created/Cancelled wake profile triggered the prompt and end check automatically. |
| Calendar strong signal | Not run | |
| App alone rejection | Not run | |
| Notification alone rejection | Not run | |
| App + notification within 10 min | Not run | |
| Cooldown duplicate suppression | Not run | |
| Manual re-arm | Not run | |
| ASR launch | Passed | `Launch ASR` opened NLL ASR from the Meet prompt. |
| Direct/auto start vs tap | Passed | ASR began recording without another tap. |
| Continued background recording | Passed | ASR continued while the Meet call remained active. |
| Stop behavior | Passed | Leaving Meet triggered one automatic `Stop and save` after the 30-second end check; the device-authored stop task cleared `%MR_ASR_ACTIVE`, returning the lifecycle to `IDLE` without a warning and saving one file. |
| Away-from-home Wi-Fi queue | Passed | With home Wi-Fi disconnected, the completed Meet recording remained queued instead of uploading. |
| Home Wi-Fi arrival upload | Passed | Rejoining home Wi-Fi cleared the queue and created exactly one Speakr recording. |
| Failed retry | Not run | Deliberately skipped; automatic/manual recovery from an attempted failed upload remains unverified. |
| Duplicate replay exactly once | Passed | Re-uploading the same Galaxy recording reused the existing Speakr recording instead of creating a duplicate. |

## Unresolved limitations

- The original six-profile `Meeting_ASR_Pixel_7.prj.xml` remains an unverified hand-assembled provenance candidate. The separate Meet lifecycle artifacts are physically validated on Pixel and Galaxy.
- The final normalized export captures Webex foreground and notification contexts as `com.cisco.wx2.android` with `com.webex.teams.WebexLauncherActivity`; behavior validation remains required.
- ASR does not provide a documented public Tasker start/stop intent. The validated lifecycle uses Tasker's `loadApp()` plus NLL's live `Stop and save` notification action, which remains device-authored.
- Notification wording and `New Only` behavior vary by app and Tasker/Android version; test the individual owner-app profiles.
- Galaxy Meet exposes an empty notification category and no `%anqueryok`; the lifecycle accepts that calibrated signature while retaining package, persistence, and `Hang Up` checks.
