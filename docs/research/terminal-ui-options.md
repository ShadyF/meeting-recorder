# Terminal UI implementation options

**Decision ticket:** [#38](https://github.com/ShadyF/meeting-recorder/issues/38)<br>
**Map:** [#36](https://github.com/ShadyF/meeting-recorder/issues/36)<br>
**Researched:** 2026-09-01<br>
**Scope:** framework and runtime constraints for an interactive, terminal-first
Publication-job manager. This note does not choose its commands, rows, columns,
destructive-action policy, or other product UX; those belong to the remaining
map tickets.

## Answer

Use **[prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/en/stable/)**
for the *interactive* layer, subject to the implementation ticket explicitly
adding and pinning its runtime dependency. Keep a separate, dependency-free,
non-interactive status renderer. This is the best balance for the current
project: it is a maintained full-screen terminal framework with a one-package
direct dependency (`wcwidth`), explicit input/output abstractions, keyboard
bindings, built-in confirmation dialogs, and progress/status facilities. The
manager will need a small project-owned table renderer, but its table is
bounded operational data rather than a spreadsheet-like widget.

This is a framework decision, not authorization to change packaging or the
runtime image in this ticket. The implementation must first decide how a pinned
wheel set is supplied to the runtime image, then make the corresponding
packaging and image changes in its own allowed scope. If the project retains an
absolute **no third-party runtime packages** rule, use `curses` instead; that is
a viable fallback, but it moves layout, focus, confirmation, resize, and
terminal-restoration work into application code.

Do not select Textual for this small manager merely for its `DataTable`, and do
not make Rich the interactive framework. Textual is capable and actively
maintained, but is substantially larger and its own roadmap still lists screen
reader integration, monochrome, high-contrast, and colour-blind themes as
future accessibility work. Rich is an excellent static table formatter, but it
does not supply a navigable full-screen application model.

## Current project and runtime constraints

The following are verified from this branch, rather than assumptions about a
generic Python CLI:

| Constraint | Evidence | Consequence for the implementation |
| --- | --- | --- |
| Application Python is `>=3.10`; project dependencies are currently empty. | [`pyproject.toml`](../../pyproject.toml) | Every third-party option is a new runtime supply-chain and update obligation. Prompt_toolkit 3.0.53 requires Python `>=3.10`; the other candidates also support the project version. |
| The Ubuntu 24.04 runtime image installs `python3` and copies source, but does not install this project with `pip`. | [`Containerfile`](../../Containerfile) and [`CONTEXT.md`](../../CONTEXT.md#runtime-image) | A dependency cannot be assumed to appear in a released image because it was present in development. A future image must install verified, pinned runtime wheels without widening the image contract unexpectedly. |
| Administrative commands are deliberately usable without the graphical daemon. | [`CONTEXT.md`](../../CONTEXT.md#runtime-image-invariants) | The manager must not import GTK or require the desktop transport, D-Bus, Pulse, or a daemon. It may require a controlling terminal only for its interactive mode. |
| Current `speakr upload --status --all` is local and secret-free, prints one JSON object per job, and its existing CLI tests use the standard library's `unittest`/stream capture. | [`meeting_recorder/__main__.py`](../../meeting_recorder/__main__.py#L585-L603), [`meeting_recorder/__main__.py`](../../meeting_recorder/__main__.py#L718-L730), and [`tests/test_speakr_cli.py`](../../tests/test_speakr_cli.py#L246-L268) | Preserve a scriptable, no-TTY path independently of the TUI. Keep state loading and action execution behind ordinary Python functions so existing unit-test style remains useful. |
| The operator journey is expected to work through `podman exec`. | [Map #36](https://github.com/ShadyF/meeting-recorder/issues/36) | Full-screen mode needs stdin and stdout attached to a controlling TTY (normally `podman exec -it …`) and a usable `TERM`/terminfo entry. A non-interactive invocation must receive stable text/JSON output, not escape sequences or a hanging prompt. |

The standard-library `curses` documentation makes the terminal constraint
concrete: `setupterm()` uses `TERM` and raises `curses.error` if the terminal or
terminfo entry cannot be read, and curses is an optional CPython module
([official documentation](https://docs.python.org/3/library/curses.html#curses.setupterm)).
Therefore implementation validation must check the *built release image*, not
just a developer shell, for both `import curses` and the actual `TERM` used by
`podman exec -it`.

## Maintained candidates considered

The release links below are primary project sources and establish current
maintenance at the research date: [Textual v8.2.8 (2026-06-30)](https://github.com/Textualize/textual/releases/tag/v8.2.8),
[prompt_toolkit 3.0.53 (2026-07-26)](https://github.com/prompt-toolkit/python-prompt-toolkit/releases/tag/3.0.53),
[Urwid 4.0.13 (2026-08-25)](https://github.com/urwid/urwid/releases/tag/4.0.13),
and [Rich v15.0.0 (2026-04-12)](https://github.com/Textualize/rich/releases/tag/v15.0.0).

| Option | Readable table and navigation | Confirmations and feedback | TTY/container behaviour | Accessibility posture | Runtime and testing trade-off | Assessment |
| --- | --- | --- | --- | --- | --- | --- |
| **stdlib `curses`** | Windows, pads, colour capability checks, special-key handling, and resizing are available, but the table layout, selection/focus, clipping, and all key bindings are project work. The [official API](https://docs.python.org/3/library/curses.html) also warns that screen state is commonly shared and not thread-safe. | Entirely project-built. `curses.wrapper()` does restore terminal modes on exceptions, which removes one failure mode but not dialog/feedback work ([docs](https://docs.python.org/3/library/curses.html#curses.wrapper)). | Lowest package footprint when the distribution provides the optional module and matching terminfo. Fails on a missing TTY/`TERM` entry unless explicitly guarded. | No project-provided screen-reader claim. Treat colour as optional; provide visible focus, text state, predictable keys, and a non-TUI output path. | No Python dependency, but the highest bespoke UI and PTY-test cost. Unit-test reducer/render functions separately; exercise terminal mode and resize paths through PTY smoke tests. | **Fallback only** if no third-party runtime package is accepted. |
| **prompt_toolkit** | Full-screen applications have a layout, focus, scrolling windows, and explicit global/control key bindings ([full-screen guide](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/full_screen_apps.html)). It has no ready-made data grid, so render the small row set with `FormattedTextControl`/`Window` and manage the selected row by stable job ID. This is deliberate, controllable work rather than a missing capability. | Provides `yes_no_dialog()`, message and button dialogs ([official dialogs guide](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/dialogs.html)); its progress API supports labelled progress and custom key bindings ([official progress guide](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/progress_bars.html)). | Its `Application` abstracts stdin/stdout as `Input`/`Output`; normal defaults use the terminal ([I/O model](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/full_screen_apps.html#i-o-objects)). Still require an explicit no-TTY fallback before constructing the application. | No verified screen-reader support found in its primary documentation. It does support Unicode double-width characters and keyboard-only operation, but that is not equivalent to assistive-technology support. | Current 3.0.53 has one direct runtime dependency, `wcwidth>=0.1.4`, and requires Python `>=3.10` ([project metadata](https://raw.githubusercontent.com/prompt-toolkit/python-prompt-toolkit/main/pyproject.toml)). Its injectable I/O model enables deterministic in-process keyboard/resize tests alongside existing `unittest` tests. | **Recommended**, once a pinned runtime supply method is approved. |
| **Urwid** | Its `ListBox` supplies focus and keyboard scrolling; `Columns` supplies horizontal layout, but a true row/column data table remains composition/custom-widget work ([widgets manual](https://urwid.org/manual/widgets.html)). | Buttons, overlays, and status widgets can form confirmations and feedback, but the dialog flow is assembled by the application. | Console display modules translate terminal escape sequences and input ([overview](https://urwid.org/manual/overview.html)); requires the same `-it`, `TERM`, and non-TTY fallback policy. | It supports monochrome through true-colour modes, plus focus maps ([display attributes](https://urwid.org/manual/displayattributes.html)); that is useful for contrast but is not a documented screen-reader guarantee. | Lightweight, but current dependencies are `wcwidth>=0.4` and `typing-extensions` ([requirements](https://raw.githubusercontent.com/urwid/urwid/master/requirements.txt)); current license is LGPL-2.1-only ([metadata](https://raw.githubusercontent.com/urwid/urwid/master/pyproject.toml)), requiring license-compliance review. Tests can render widgets and drive `MainLoop`, but project-owned composition remains sizeable. | Credible alternative, but less direct confirmation/feedback support and less favourable licensing for this narrow need. |
| **Textual** | `DataTable` is a focused table with keyboard row/cell navigation, selection messages, scrolling, fixed rows/columns, and Rich renderables ([official widget documentation](https://textual.textualize.io/widgets/data_table/)). This is the strongest ready-made table. | Framework widgets and application state make dialogs, notifications, and live feedback straightforward. | Runs in a terminal, but it is still a full-screen terminal application; require `-it`, valid terminal capabilities, and an independently rendered no-TTY mode. | Do **not** claim mature accessibility: Textual's official [roadmap](https://textual.textualize.io/roadmap/) lists screen-reader integration, monochrome, high-contrast, and colour-blind themes as planned work. | Six direct packages before optional syntax extras: `rich`, `markdown-it-py`, `mdit-py-plugins`, `platformdirs`, `typing-extensions`, and `pygments` ([metadata](https://raw.githubusercontent.com/Textualize/textual/main/pyproject.toml)). Excellent headless interaction testing through `App.run_test()`/`Pilot`, but tests are async and commonly add pytest/pytest-asyncio ([testing guide](https://textual.textualize.io/guide/testing/)). | Technically strong but **not proportionate** to this dependency-free runtime and insufficiently established for accessibility claims. Revisit only if a later ticket requires a rich, extensible data-grid UI. |
| **Rich** | Produces highly readable static tables with width calculation, wrapping, ASCII-safe boxes, alignment, and alternate row styles ([official tables guide](https://rich.readthedocs.io/en/stable/tables.html)). It has no full-screen focus or keyboard-navigation model. | Progress and formatted messages are strong for one-shot command feedback; confirmations and interactive selection are outside its table API. | Works well for normal or redirected stdout, so it is a plausible *static-output* helper, but it does not solve the `podman exec -it` manager. | Colour and Unicode should be optional; `safe_box=True` can force ASCII. No screen-reader guarantee was found in primary documentation. | Rich 15.0.0 directly needs `pygments` and `markdown-it-py` ([metadata](https://raw.githubusercontent.com/Textualize/rich/master/pyproject.toml)), plus their transitive dependencies. Static rendering is easy to snapshot/assert, but an additional interaction framework would still be required. | **Do not use as the manager framework.** Also avoid it solely for the non-interactive table unless its added footprint is separately justified. |

## Recommendation details and boundaries

### Recommended implementation boundary

1. **Keep the Publication-job application boundary framework-free.** A
   presentation model should contain only safe, public fields and stable job
   identity; action adapters should call the existing publisher/cleanup
   operations. Neither the manager nor its renderer should duplicate
   Publication-job state transitions, authentication, SSID admission, or
   Publication cleanup invariants.
2. **Render non-interactive output without prompt_toolkit.** Detect unsuitable
   standard streams before importing/starting the full-screen application and
   use a deterministic text/JSON renderer. This keeps piping, redirection, and
   automation available and avoids an alternate-screen escape sequence in logs.
   It also maintains the map's stated readable non-interactive table goal.
3. **Use prompt_toolkit only after both streams are TTYs and terminal setup is
   viable.** Its full-screen layout, focus/key-binding model, yes/no dialog, and
   progress/status capabilities cover the required interaction mechanics. Keep
   a selected row as a job ID, not a display offset, so refresh, sort, or
   deletion cannot direct an operation at a different Publication job.
4. **Treat every state-changing call as a model refresh point.** Display the
   authoritative result/error after the call and restore focus by stable ID if
   it still exists. The exact confirmation wording, operation list, and
   destructive-action rules remain other tickets' decisions.

This boundary lets a future implementation substitute `curses` if dependency
approval fails, without changing the Publication-job application interface or
the non-interactive output contract.

### Terminal and container contract

The implementation ticket should make these mechanics explicit rather than
relying on a framework exception:

- Interactive mode requires `stdin.isatty()` and `stdout.isatty()`, a non-empty
  `TERM`, and a matching terminfo entry in the runtime image. The supported
  invocation is the operator's `podman exec -it …` form; an invocation without
  `-it`, through a pipe, or with unusable terminal capabilities must return the
  non-interactive renderer or a clear, non-secret error without waiting for
  input.
- Do not start a terminal UI in the recorder daemon. The manager is an explicit
  administrative command and must remain usable without the graphical runtime
  host contract.
- Always leave the terminal usable on exceptions, cancellation, `SIGINT`, and
  resize. This is intrinsic with prompt_toolkit's lifecycle, but still needs
  tests; it is a larger manual obligation for raw curses.
- Do not use colour, box glyphs, mouse operation, terminal width, or a
  transient toast as the only conveyance of state. Use labels, a keyboard
  visible focus indicator, a persistent status/message area, ASCII-safe
  fallback characters, and a narrow-terminal layout that preserves identity
  and state over decoration.

### Packaging and supply-chain decision required later

The existing `Containerfile` intentionally has no Python package-install step.
For prompt_toolkit, the implementation owner must choose and document one
reproducible release method before adding code that imports it:

- add exact application dependency pins and install their verified wheels in
  the build image, or
- deliberately vendor an audited, versioned dependency set and document its
  update process.

The first is conventional and easier to audit; the second avoids a build-time
index dependency but creates a heavier manual update burden. Neither is in the
scope of this research ticket. In both cases, record direct and transitive
licenses, hashes/source provenance, and a supported upgrade test. Do not add
an unpinned `pip install prompt_toolkit` during container startup.

## Accessibility and feedback constraints

Terminal applications cannot reliably provide the semantic tree expected by
screen readers merely by adding a framework. None of the candidates above has a
verified primary-source screen-reader guarantee; Textual explicitly treats it
as roadmap work. The implementation therefore needs an accessible operational
baseline that works with ordinary terminal copying, magnification, high/low
colour settings, and automation:

- Give every state a short text label; never encode it only with colour, emoji,
  border weight, position, or a progress glyph.
- Keep keyboard navigation complete and document the currently available
  bindings in the terminal itself; do not make mouse support necessary.
- Put confirmation target identity and consequence in selectable text, choose
  the safe non-action by default, and retain a status line after completion or
  failure long enough to inspect/copy it.
- Preserve a concise non-interactive textual/JSON representation for operators
  who cannot or do not use the full-screen mode.

These are implementation constraints, not a decision about the manager's
specific actions or visual design.

## Validation hand-off for the implementation ticket

No application implementation is performed by this research ticket. When the
orchestrator's implementation work starts, validate the selected approach at
three layers:

1. **Existing behaviour:** retain and extend standard-library unit tests around
   presentation-model construction and action adapters. Assert that local
   inspection does not read secrets or contact the network, matching the
   existing status tests.
2. **Interactive mechanics:** with prompt_toolkit's controlled input/output,
   test navigation, selection persistence by job ID, cancellation/default
   confirmation, success/failure feedback, refresh after a mutation, narrow
   sizes, Unicode width, and resize. Do not make framework screenshot tests the
   sole proof of behaviour.
3. **Released-container smoke:** from the built runtime image, verify an
   interactive `podman exec -it` session using the intended `TERM`, then verify
   no-TTY, piped, redirected, missing/invalid `TERM`, and `SIGINT` cases. Check
   that output contains no bearer token, private path, or unintended terminal
   control sequence in the non-interactive path.

This research resolves the framework choice and constraints only. It leaves
the Publication-job actions, exact data presentation, output compatibility,
confirmation policy, migration work, and user documentation to their assigned
map tickets.
