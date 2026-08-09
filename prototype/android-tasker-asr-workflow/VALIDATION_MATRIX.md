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
| In-call playback / background recording | Unknown | Not retested; timer/state alone do not establish captured Meet audio. |
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
| Direct/auto start vs tap | Passed | ASR began its timer/service without another tap; this did not produce in-call audio. |
| In-call/background recording acceptance | Failed | NLL captured no audio throughout the active Meet call despite its timer/service running: Meet-muted and Meet-unmuted segments were silent; audio was audible only after leaving. |
| Physical order reversal | Failed | NLL started before Meet captured before the call, became blank immediately on joining, and resumed after leaving; this rules out Tasker timing and NLL permissions. |
| Stop behavior | Passed | Leaving Meet triggered one automatic `Stop and save` after the 30-second end check; the device-authored stop task cleared `%MR_ASR_ACTIVE`, returning the lifecycle to `IDLE` without a warning and saving one file, not proof of in-call audio. |
| Away-from-home Wi-Fi queue | Passed | With home Wi-Fi disconnected, the completed file remained queued instead of uploading; it may contain only post-call audio and does not satisfy meeting-recording acceptance. |
| Home Wi-Fi arrival upload | Passed | Rejoining home Wi-Fi cleared the queue and created exactly one Speakr recording; the upload may contain only post-call audio and does not satisfy meeting-recording acceptance. |
| Failed retry | Not run | Deliberately skipped; automatic/manual recovery from an attempted failed upload remains unverified. |
| Duplicate replay exactly once | Passed | Re-uploading the same Galaxy file reused the existing Speakr recording instead of creating a duplicate; replay does not establish in-call audio. |

## Unresolved limitations

- The original six-profile `Meeting_ASR_Pixel_7.prj.xml` remains an unverified hand-assembled provenance candidate. Galaxy lifecycle automation is physically validated, but its NLL Meet recording fails; Pixel in-call playback is unknown and not retested.
- The final normalized export captures Webex foreground and notification contexts as `com.cisco.wx2.android` with `com.webex.teams.WebexLauncherActivity`; behavior validation remains required.
- ASR does not provide a documented public Tasker start/stop intent. The validated lifecycle uses Tasker's `loadApp()` plus NLL's live `Stop and save` notification action, which remains device-authored.
- Notification wording and `New Only` behavior vary by app and Tasker/Android version; test the individual owner-app profiles.
- Galaxy Meet exposes an empty notification category and no `%anqueryok`; the lifecycle accepts that calibrated signature while retaining package, persistence, and `Hang Up` checks.
- Galaxy order-reversal playback confirms Android voice-communication microphone priority/silencing: ASR started before Meet captures audio before the call, becomes blank immediately on joining, and resumes after leaving. Meet has input priority over the ordinary recorder; this is not a Tasker timing bug or NLL permission issue. [Android audio-input sharing](https://developer.android.com/media/platform/sharing-audio-input) and [concurrent audio capture](https://source.android.com/docs/core/audio/concurrent) apply.
- Do not rely on or advertise Galaxy Meet recording through NLL. Same-tablet Tasker/NLL has no supported workaround: muting Meet, starting NLL first, Samsung Mic mode, Separate App Sound, screen recording, or extra Tasker permissions do not override audio policy. Use Meet native recording where available and consented, or a second physical recorder. Issue #10 acceptance remains open and unmet.
