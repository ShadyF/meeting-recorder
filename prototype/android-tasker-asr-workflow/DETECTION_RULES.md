# Detection rules

**Prototype only.** This Android workflow is independent of the Linux recorder. The six-profile Pixel candidate below detects context and asks the user to open ASR. The separately validated Galaxy Meet lifecycle launches, records, and stops through device-authored Tasker actions.

## Validated Galaxy Meet lifecycle

Import `MR_Meet_Lifecycle.prj.xml` and the normalized Galaxy standalone exports for `MR Inspect Meetings`, `Launch ASR`, `MR Stop ASR`, and `MR Meet Posted Wake`. Keep exactly one profile named **`MR Meet Posted Wake`**; do not rename it. Its AutoNotification status is **both Created and Cancelled** and it performs **`MR Meet Reconcile`** with no parameters.

The Galaxy Meet notification signature is package `com.google.android.apps.tachyon`, empty category, persistent `true`, and button 1 `Hang Up`. `%anqueryok` may be unset, so it is not a required match. `MR Stop ASR` presses NLL **Stop and save** and then sets `%MR_ASR_ACTIVE=0`; the validated end path returns to `IDLE` with one saved file.

The rules below are retained as provenance for the older six-profile Pixel candidate, not as the Galaxy lifecycle configuration.

## Signals and state

| Signal | Tasker source | Strength | Stored state |
| --- | --- | --- | --- |
| Calendar | One selected active `Calendar Entry` state | Strong | Passed to Evaluate as `%par1 = calendar` |
| Foreground app | One `Application` state containing Meet, Zoom, Webex, and Teams | Weak | `%MR_APP_AT = %TIMES` |
| Notification | Four `Notification` events, one per owner app, with `New Only` where offered | Weak | `%MR_NOTIFICATION_AT = %TIMES` |

Allowed apps:

| App | Package |
| --- | --- |
| Google Meet | `com.google.android.apps.tachyon` |
| Zoom | `us.zoom.videomeetings` |
| Webex | `com.cisco.webex.meetings` |
| Microsoft Teams | `com.microsoft.teams` |

## Decision sequence

`Evaluate` is the only task that decides whether to prompt.

### `Evaluate` actions, in order

Use these Tasker actions in this order. The condition editor's labels may differ slightly by build.

1. **Variable Set** — Name: `%window_start`; To: `%TIMES - 600`; enable **Do Maths**.
2. **If** — `%MR_COOLDOWN_UNTIL` is set **AND** `%MR_COOLDOWN_UNTIL > %TIMES`.
3. **Stop**.
4. **End If**.
5. **If** — `%par1` matches `calendar`.
6. **Perform Task** — `Prompt`.
7. **Stop**.
8. **End If**.
9. **If** — `%MR_APP_AT` is set **AND** `%MR_NOTIFICATION_AT` is set **AND** `%MR_APP_AT >= %window_start` **AND** `%MR_NOTIFICATION_AT >= %window_start`.
10. **Perform Task** — `Prompt`.
11. **End If**.

The last condition is the ten-minute weak-signal correlation window; either weak signal alone is rejected. A selected active calendar event is sufficient by itself.

### `Prompt` actions, in order

1. **Variable Set** — Name: `%MR_COOLDOWN_UNTIL`; To: `%TIMES + 2700`; enable **Do Maths**.
2. **Notify** — Title: `Meeting detected`; Text: `Open ASR to record`; add an `Open ASR` action button that performs `Launch ASR`.

The cooldown is 45 minutes and is checked before all detection rules. Tasker's same-title **Notify** replaces the prior notification; the cooldown, not replacement behavior, is the primary duplicate-prompt guard.

### `Manual Re-arm` actions, in order

1. **Variable Clear** — `%MR_COOLDOWN_UNTIL`.
2. **Variable Clear** — `%MR_APP_AT`.
3. **Variable Clear** — `%MR_NOTIFICATION_AT`.

Do not use **Variable Add** for these calculations.

## Task map

| Task | Actions |
| --- | --- |
| `Signal Calendar` | `Perform Task` → `Evaluate`, Parameter 1: `calendar` |
| `Signal App` | `Variable Set` `%MR_APP_AT` to `%TIMES`; `Perform Task` → `Evaluate` |
| `Signal Notification` | `Variable Set` `%MR_NOTIFICATION_AT` to `%TIMES`; `Perform Task` → `Evaluate` |
| `Evaluate` | Use the ordered actions above; task collision handling: **Abort Existing Task** so the newest signal evaluates combined state |
| `Prompt` | Use the ordered actions above; task collision handling: **Abort New Task** |
| `Launch ASR` | `Launch App` → **ASR Voice Recorder** (`com.nll.asr`) |
| `Manual Re-arm` | Use the ordered actions above; optionally add this task as a home-screen shortcut |

## Boundaries

- The app and notification signals do not have to come from the same app; this is intentionally a low-cost prototype heuristic.
- A profile event can arrive more than once. The cooldown is the duplicate-prompt guard.
- This workflow has no reliable, documented Tasker action to start or stop ASR recording silently. Opening ASR may require a user tap to start recording.
