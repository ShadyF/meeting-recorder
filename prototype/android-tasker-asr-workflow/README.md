# Meeting ASR: Pixel 7 Tasker prototype

**Throwaway Android-only prototype.** It is independent of the Linux recorder. Never put a real API secret in this repository or in screenshots.

Target device: **Pixel 7**, Android 17 `CP2A.260705.006`, Tasker `6.6.20`, and the latest installed **ASR Voice Recorder** (`com.nll.asr`). The expected meeting apps are Meet (`com.google.android.apps.tachyon`), Zoom (`us.zoom.videomeetings`), Webex (`com.cisco.wx2.android`), and Teams (`com.microsoft.teams`).

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

Tasker can launch ASR, but no documented public ASR intent here promises a silent start or stop. The `Open ASR` button may open ASR and require one user tap to begin recording. Test background-start behavior on the device; Android may block launches without user interaction.

## Export and capture

The verified file is the captured Tasker 6.6.20 normalized re-export. Preserve the original candidate as provenance; do not treat import success as behavior validation.

Run the cases in [VALIDATION_MATRIX.md](VALIDATION_MATRIX.md) before treating this prototype as usable.

## Sources and tracking

- [Tasker user guide / action index](https://tasker.joaoapps.com/userguide/en/help/ah_index.html) and [Tasker Android power guidance](https://tasker.joaoapps.com/userguide/en/androidpowermanagement.html)
- [Android background activity starts](https://developer.android.com/guide/components/activities/background-starts) and [microphone foreground-service restrictions](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start)
- [NLL ASR Voice Recorder](https://nllapps.com/apps/asr/) and [ASR cloud/WebHook documentation](https://nllapps.com/common/cloud2/)
- [Samsung battery optimization guidance](https://www.samsung.com/us/support/answer/ANS10002588/) (relevant when validating the Galaxy)
- [Speakr ASR upload contract tests](https://github.com/murtaza-nasir/speakr/blob/v0.10.3-alpha/tests/test_asr_voice_recorder_upload.py) and [repository issue tracker](https://github.com/ShadyF/meeting-recorder/issues)
