# Meeting ASR: Tasker prototype

**Throwaway Android-only prototype.** It is independent of the Linux recorder. Never put a real API secret in this repository or in screenshots.

The original candidate targets a **Pixel 7**, Android 17 `CP2A.260705.006`, Tasker `6.6.20`, and the latest installed **ASR Voice Recorder** (`com.nll.asr`). Its expected meeting apps are Meet (`com.google.android.apps.tachyon`), Zoom (`us.zoom.videomeetings`), Webex (`com.cisco.wx2.android`), and Teams (`com.microsoft.teams`). The separate Meet lifecycle automation is physically validated on the Galaxy Tab S9, Android 16 `BP4A.251205.006.X710XXS6EZG3`, with Tasker `6.6.20`.

## Galaxy Meet lifecycle

> **Warning:** Do not rely on or advertise Galaxy Meet recording through NLL ASR. On the Galaxy Tab S9 (Android 16 `BP4A.251205.006.X710XXS6EZG3`, Tasker 6.6.20), Tasker detection, prompt, launch, auto-stop, state, queue, and upload mechanics work, but NLL captures silence throughout an active Meet call despite its timer/service running. Meet-muted and Meet-unmuted segments are silent; audio is audible only after leaving. Starting NLL before Meet records before the call, becomes blank immediately on joining, and resumes after leaving. Android voice-communication microphone priority/silencing—not Tasker timing or NLL permissions—is the cause ([audio-input sharing](https://developer.android.com/media/platform/sharing-audio-input), [concurrent audio capture](https://source.android.com/docs/core/audio/concurrent)). Same-tablet Tasker/NLL has no supported workaround: muting Meet, starting NLL first, Samsung Mic mode, Separate App Sound, screen recording, or extra Tasker permissions do not override audio policy. Use consented Google Meet native recording where available, or a second physical recorder. Issue #10 acceptance remains open and unmet.

Import `MR_Meet_Lifecycle.prj.xml`, grant Tasker notification access and the permissions it requests, and keep Tasker and ASR Voice Recorder unrestricted for battery use. Then import the normalized device-authored `MR Inspect Meetings`, `Launch ASR`, and `MR Stop ASR` tasks plus the `MR Meet Posted Wake` profile; do not substitute the older Pixel candidate tasks.

There is exactly one wake profile: **`MR Meet Posted Wake`**. Its AutoNotification status is **both Created and Cancelled**, and it performs **`MR Meet Reconcile` with no parameters**. Do not rename the profile. Its calibrated Galaxy Meet signature is package `com.google.android.apps.tachyon`, empty category, persistent `true`, and button 1 `Hang Up`; `%anqueryok` may be unset.

The validated automation lifecycle launches ASR and automatically stops after the Meet end check. `MR Stop ASR` presses NLL **Stop and save**, then sets `%MR_ASR_ACTIVE=0`, returning the lifecycle to `IDLE` and saving one file; that file is not evidence of in-call audio.

## Import on the Pixel

Use **`Meeting_ASR_Pixel_7_verified.prj.xml`** for subsequent Pixel imports. It was imported and re-exported by Tasker 6.6.20 on the Pixel 7, Android 17 `CP2A.260705.006`. `Meeting_ASR_Pixel_7.prj.xml` remains the unverified hand-assembled candidate kept as provenance. Tasker has no official stable XML schema, so behavior validation is still required. Labels can differ slightly by Tasker build.

1. In Tasker, use project import and select `Meeting_ASR_Pixel_7_verified.prj.xml`.
2. Before enabling anything, inspect the six profiles (`MR Calendar Test`, `MR Meeting Apps`, and four `MR Notify …` profiles) and six tasks (`Signal Calendar`, `Signal App`, `Signal Notification`, `Prompt`, `Launch ASR`, `Manual Re-arm`).
3. Grant Tasker the prompts it requests: **Calendar**, **Notification access**, **Accessibility/usage access** if requested for the selected contexts, and **Notifications**. In Android battery settings, set **Tasker** and **ASR Voice Recorder** to **Unrestricted**.
4. The Calendar profile imports with title `*MR TEST*` and a wildcard calendar. Select the intended calendar after import, create an active event named `MR TEST Pixel`, and keep this test filter to avoid prompting on every event. After validation, replace it with the user's chosen meeting-title rule.
5. The final normalized foreground **Application** profile contains all four apps. Webex is captured as `com.cisco.wx2.android` with `com.webex.teams.WebexLauncherActivity`. Check each Notification owner context after import. For first controlled validation, leave notification title/text filters blank; Owner Application and New Only constrain them while testing whether per-app text filters are viable.
6. If a subsequent import fails, report the exact Tasker import error before changing either XML file.

## Manual fallback

Only use this path if direct import fails. It uses the original seven-task construction in [DETECTION_RULES.md](DETECTION_RULES.md#task-map), including `Evaluate`, rather than recreating the candidate's embedded JavaScript logic. Set `Evaluate` to **Abort Existing Task** and `Prompt` to **Abort New Task** under collision handling. Use the same dedicated-calendar `*MR TEST*` / `MR TEST Pixel` validation filter, then replace it after validation. Keep one App context containing the four meeting apps, and four owner-specific Notification event contexts with New Only where available and blank title/text filters initially.

The rule values and Tasker expressions are deliberately specified in [DETECTION_RULES.md](DETECTION_RULES.md): use `%TIMES`, `%MR_APP_AT`, `%MR_NOTIFICATION_AT`, `%MR_COOLDOWN_UNTIL`, and local `%window_start`; use **Variable Set** with **Do Maths**, not Variable Add.

## Configure ASR Voice Recorder

1. Grant ASR **Microphone** and **Notifications** permissions, then verify that it can continue recording in the background. Keep ASR battery use **Unrestricted**.
2. Configure its cloud/WebHook upload destination (menu wording may differ by ASR version):
   - URL: `https://SPEAKR_HOST/api/v1/integrations/asr-voice-recorder/upload`
   - secret: `SPEAKR_TEST_TOKEN` (placeholder only; replace privately on the device)
   - enable **Wi-Fi-only** uploads;
   - disable **Auto disconnect** for a home-only endpoint.
3. Expect ASR to attempt an upload about 30–60 seconds after a recording and to retry queued failures about every 1–2 hours. The ASR queue can also be uploaded manually.

The legacy Pixel candidate can launch ASR, but no documented public ASR intent promises a silent start or stop. The validated Galaxy Meet lifecycle instead uses the device-authored NLL notification action for **Stop and save**.

## Export and capture

The verified Pixel file is the captured Tasker 6.6.20 normalized re-export. Preserve the original six-profile candidate as provenance; do not treat import success as behavior validation. The Galaxy standalone exports are separately device-authored and normalized when captured.

Run the cases in [VALIDATION_MATRIX.md](VALIDATION_MATRIX.md) before treating this prototype as usable.

## Sources and tracking

- [Tasker user guide / action index](https://tasker.joaoapps.com/userguide/en/help/ah_index.html) and [Tasker Android power guidance](https://tasker.joaoapps.com/userguide/en/androidpowermanagement.html)
- [Android background activity starts](https://developer.android.com/guide/components/activities/background-starts), [microphone foreground-service restrictions](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start), [audio-input sharing](https://developer.android.com/media/platform/sharing-audio-input), and [concurrent audio capture](https://source.android.com/docs/core/audio/concurrent)
- [NLL ASR Voice Recorder](https://nllapps.com/apps/asr/) and [ASR cloud/WebHook documentation](https://nllapps.com/common/cloud2/)
- [Samsung battery optimization guidance](https://www.samsung.com/us/support/answer/ANS10002588/)
- [Speakr ASR upload contract tests](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/tests/test_asr_voice_recorder_upload.py) and [repository issue tracker](https://github.com/ShadyF/meeting-recorder/issues)
