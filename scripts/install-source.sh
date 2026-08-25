#!/usr/bin/env bash
# Install or remove the user-local desktop integration for this source checkout.
set -euo pipefail

# Keep the command surface small so activation is always explicit.
MODE=install
ENABLE=0
case "${1:-}" in
    "") ;;
    --enable) ENABLE=1 ;;
    --remove|--uninstall) MODE=remove ;;
    --help|-h)
        printf 'Usage: %s [--enable|--remove]\n' "${BASH_SOURCE[0]}"
        exit 0
        ;;
    *)
        printf 'ERROR: unknown option: %s\n' "$1" >&2
        exit 2
        ;;
esac

# Resolve this checkout before writing any user-local integration files.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"
ROOT="$(cd -- "$ROOT" && pwd -P)"
[[ -f "$ROOT/meeting_recorder/__main__.py" ]] || {
    printf 'ERROR: checkout does not contain meeting_recorder\n' >&2
    exit 1
}

# Use standard XDG roots and reject relative values before they become write targets.
HOME_DIR="${HOME:?HOME must be set}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME_DIR/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME_DIR/.local/share}"
STATE_HOME="${XDG_STATE_HOME:-$HOME_DIR/.local/state}"
for directory in "$HOME_DIR" "$CONFIG_HOME" "$DATA_HOME" "$STATE_HOME"; do
    [[ "$directory" == /* ]] || {
        printf 'ERROR: XDG and HOME paths must be absolute\n' >&2
        exit 1
    }
done

LAUNCHER="$HOME_DIR/.local/bin/meeting-recorder"
UNIT_LINK="$CONFIG_HOME/systemd/user/meeting-recorder.service"
DESKTOP_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
STATE_FILE="$STATE_HOME/meeting-recorder/source-install"
ASSETS=(
    "meeting-recorder.desktop"
    "meeting-recorder-settings.desktop"
    "meeting-recorder.svg"
    "meeting-recorder-recording.svg"
    "meeting-recorder-paused.svg"
)

# Read the installer state only when its bounded two-line format is intact.
state_root=""
if [[ -e "$STATE_FILE" || -L "$STATE_FILE" ]]; then
    [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || {
        printf 'ERROR: installer state conflicts with a non-regular file: %s\n' "$STATE_FILE" >&2
        exit 1
    }
    mapfile -t state_lines < "$STATE_FILE"
    [[ ${#state_lines[@]} == 2 && "${state_lines[0]}" == '# Managed by meeting-recorder source installer.' && "${state_lines[1]}" == /* ]] || {
        printf 'ERROR: installer state has an unexpected format: %s\n' "$STATE_FILE" >&2
        exit 1
    }
    state_root="${state_lines[1]}"
fi

# Accept only links that the prior installer pointed at its recorded checkout.
managed_link() {
    local path="$1"
    local relative="$2"
    [[ -n "$state_root" && -L "$path" && "$(readlink -- "$path")" == "$state_root/$relative" ]]
}

# Recognize launcher files written by this installer without treating other files as replaceable.
managed_launcher() {
    local -a launcher_lines
    [[ -f "$LAUNCHER" && ! -L "$LAUNCHER" ]] || return 1
    mapfile -t launcher_lines < "$LAUNCHER"
    [[ ${#launcher_lines[@]} -ge 2 && "${launcher_lines[0]}" == '#!/usr/bin/env bash' && "${launcher_lines[1]}" == '# Managed by meeting-recorder source installer.' ]]
}

# Check every destination before changing anything so caller-owned files remain untouched.
check_destinations() {
    local asset destination relative
    if [[ -e "$LAUNCHER" || -L "$LAUNCHER" ]]; then
        managed_launcher || {
            printf 'ERROR: launcher conflicts with a caller-owned file: %s\n' "$LAUNCHER" >&2
            return 1
        }
    fi
    if [[ -e "$UNIT_LINK" || -L "$UNIT_LINK" ]]; then
        managed_link "$UNIT_LINK" 'systemd/meeting-recorder.service' || {
            printf 'ERROR: service link conflicts with a caller-owned file: %s\n' "$UNIT_LINK" >&2
            return 1
        }
    fi
    for asset in "${ASSETS[@]}"; do
        if [[ "$asset" == *.desktop ]]; then
            destination="$DESKTOP_DIR/$asset"
        else
            destination="$ICON_DIR/$asset"
        fi
        relative="packaging/$asset"
        if [[ -e "$destination" || -L "$destination" ]]; then
            managed_link "$destination" "$relative" || {
                printf 'ERROR: desktop asset conflicts with a caller-owned file: %s\n' "$destination" >&2
                return 1
            }
        fi
    done
}

# Write the launcher through a private temporary file so it is never partially executable.
write_launcher() {
    local temporary
    mkdir -p -- "$(dirname -- "$LAUNCHER")"
    temporary="$(mktemp "$(dirname -- "$LAUNCHER")/.meeting-recorder.XXXXXX")"
    trap 'rm -f -- "$temporary"' RETURN
    {
        printf '%s\n' '#!/usr/bin/env bash'
        printf '%s\n' '# Managed by meeting-recorder source installer.'
        printf '%s\n' 'set -euo pipefail'
        printf 'cd -- %q\n' "$ROOT"
        printf '%s\n' 'exec python3 -m meeting_recorder "$@"'
    } > "$temporary"
    chmod 755 -- "$temporary"
    mv -f -- "$temporary" "$LAUNCHER"
    trap - RETURN
}

# Replace only the installer-managed links after the full conflict check succeeds.
install_links() {
    local asset destination
    mkdir -p -- "$(dirname -- "$UNIT_LINK")" "$DESKTOP_DIR" "$ICON_DIR"
    ln -sfn -- "$ROOT/systemd/meeting-recorder.service" "$UNIT_LINK"
    for asset in "${ASSETS[@]}"; do
        if [[ "$asset" == *.desktop ]]; then
            destination="$DESKTOP_DIR/$asset"
        else
            destination="$ICON_DIR/$asset"
        fi
        ln -sfn -- "$ROOT/packaging/$asset" "$destination"
    done
}

# Record the current checkout only after all links and the launcher are installed.
write_state() {
    local temporary
    mkdir -p -- "$(dirname -- "$STATE_FILE")"
    temporary="$(mktemp "$(dirname -- "$STATE_FILE")/.source-install.XXXXXX")"
    trap 'rm -f -- "$temporary"' RETURN
    printf '%s\n%s\n' '# Managed by meeting-recorder source installer.' "$ROOT" > "$temporary"
    chmod 600 -- "$temporary"
    mv -f -- "$temporary" "$STATE_FILE"
    trap - RETURN
}

# Remove only destinations that still point to the checkout recorded by the installer.
remove_links() {
    local asset destination relative
    managed_launcher && rm -f -- "$LAUNCHER"
    managed_link "$UNIT_LINK" 'systemd/meeting-recorder.service' && rm -f -- "$UNIT_LINK"
    for asset in "${ASSETS[@]}"; do
        if [[ "$asset" == *.desktop ]]; then
            destination="$DESKTOP_DIR/$asset"
        else
            destination="$ICON_DIR/$asset"
        fi
        relative="packaging/$asset"
        managed_link "$destination" "$relative" && rm -f -- "$destination"
    done
    rm -f -- "$STATE_FILE"
}

# Install links without starting the service unless the caller selected activation.
if [[ "$MODE" == install ]]; then
    check_destinations
    write_launcher
    install_links
    write_state
    systemctl --user daemon-reload
    if ((ENABLE)); then
        systemctl --user enable --now meeting-recorder.service
    fi
    printf 'Installed source integration from: %s\n' "$ROOT"
    exit 0
fi

# Stop the known managed unit before unlinking it, then reload the user manager.
[[ -n "$state_root" ]] || {
    printf 'ERROR: no source integration state exists at: %s\n' "$STATE_FILE" >&2
    exit 1
}
check_destinations
systemctl --user disable --now meeting-recorder.service || true
remove_links
systemctl --user daemon-reload
printf 'Removed source integration recorded for: %s\n' "$state_root"
