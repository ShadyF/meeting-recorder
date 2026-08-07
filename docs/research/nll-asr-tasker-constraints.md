# NLL ASR + Tasker constraints

## Confirmed facts

### ASR capabilities

NLL ASR's [Google Play listing](https://play.google.com/store/apps/details?id=com.nll.asr&hl=en) confirms that it supports:

- recording in the background;
- automatic recording start;
- a widget and shortcut;
- a customizable recording folder;
- cloud uploads; and
- Wi-Fi transfer.

ASR supports recording formats and a recording folder. Its [official site](https://nllapps.com/apps/asr/) and [privacy policy](https://nllapps.com/apps/asr/policy.htm) do not document a Tasker plugin or a public start/stop intent.

### Cloud upload behavior

The [ASR cloud documentation](https://nllapps.com/common/cloud2/) documents these destinations: Google Drive, OneDrive, Dropbox, Box, Email, FTP, FTPS, SFTP, WebDAV, WebHook, and a device folder.

For a WebHook destination, ASR sends multipart fields for the recording file, `file_name`, `date`, `duration`, and `note`, plus an optional secret. It supports Wi-Fi-only upload. A one-time upload occurs roughly 30–60 seconds after recording; periodic retries occur roughly every 1–2 hours, and the queue can be uploaded manually. Optional remote date and device folders are available. For a home-only endpoint, automatic disconnect should be disabled.

Speakr v0.10.3-alpha exposes `POST /api/v1/integrations/asr-voice-recorder/upload`, accepting `secret`, `file`, `file_name`, `date`, `note`, and `duration`. It is therefore compatible with ASR's WebHook fields and its retry/replay behavior.

### Tasker and Android limits

Tasker can [Launch App](https://tasker.joaoapps.com/userguide/en/help/ah_index.html) and [send intents](https://tasker.joaoapps.com/userguide/en/intents.html) only when the target action and extras are known.

Android restricts [background activity starts](https://developer.android.com/guide/components/activities/background-starts), [foreground-service starts from the background](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start), and [microphone foreground-service creation](https://developer.android.com/about/versions/14/changes/fgs-types-required). Samsung battery management adds device-specific uncertainty; see Tasker's [Android power-management guidance](https://tasker.joaoapps.com/userguide/en/androidpowermanagement.html).

Consequently, Tasker can reliably launch ASR from user interaction. Calendar, foreground-app, and notification profiles can detect meeting-like context, but an entirely background launch may be blocked. Once legitimately started, ASR can record in the background. Silent, unattended start and stop are not documented or reliable.

## Unknowns and unsupported assumptions

- ASR shortcut semantics and arbitrary Tasker invocation of a shortcut are undocumented.
- Configurable filename templates are unconfirmed.
- Passing a filename or other recording parameters from Tasker to ASR is unconfirmed.
- A public ASR intent action or extras for silent start/stop are not documented.
- Whether a particular Samsung device permits a background launch depends on its Android version and battery-management configuration.

## Smallest viable workflow

1. Configure the ASR recording profile, permissions, and local recording folder.
2. Configure Speakr's ASR WebHook endpoint in ASR with its secret.
3. Enable Wi-Fi-only uploads and ASR retries; leave automatic disconnect disabled for a home-only endpoint.
4. Use Tasker to combine Calendar, foreground-app, and notification heuristics. Have it show a prompt or launch ASR while the user is interacting with the device.
5. Let the user tap record when Android or the device does not permit the launch to begin recording directly.
6. ASR records in the background and uploads to Speakr when home Wi-Fi is available; ASR's upload queue retries failed transfers.

This workflow deliberately relies on the documented ASR WebHook queue rather than undocumented Tasker intent or shortcut behavior.
