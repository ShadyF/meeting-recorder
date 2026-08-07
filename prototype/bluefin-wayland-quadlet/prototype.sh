#!/usr/bin/env bash
# THROWAWAY PROTOTYPE ONLY. This script manages no application unit or data.

# Stop on errors, unset variables, and failed pipeline commands.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
IMAGE=localhost/meeting-recorder-bluefin-prototype:latest
UNIT=meeting-recorder-bluefin-prototype.container
SERVICE=meeting-recorder-bluefin-prototype.service
PROTOTYPE_HOME="${HOME}/.local/share/meeting-recorder-bluefin-prototype"
CONFIG_DIR="${PROTOTYPE_HOME}/config"
CONFIG_FILE="${CONFIG_DIR}/meeting-recorder/config.json"
RECORDINGS_DIR="${PROTOTYPE_HOME}/Recordings"
QUADLET_DIR="${HOME}/.config/containers/systemd"
QUADLET_DEST="${QUADLET_DIR}/${UNIT}"

usage() {
    # Print the supported lifecycle actions for invalid invocations.
    printf 'Usage: %s {up|status|logs|down}\n' "${0##*/}"
}

up() {
    # Create isolated configuration and evidence directories for this prototype.
    install -d -m 700 "${CONFIG_DIR}/meeting-recorder" "${RECORDINGS_DIR}"

    # Keep a user-edited prototype configuration instead of overwriting it.
    if [[ ! -e "${CONFIG_FILE}" ]]; then
        printf '%s\n' '{' '  "output_dir": "/home/meeting-recorder/Recordings"' '}' > "${CONFIG_FILE}"
    fi

    # Build from the repository root so the Containerfile can copy the application package.
    podman build --tag "${IMAGE}" --file "${SCRIPT_DIR}/Containerfile" "${REPO_ROOT}"

    # Install only this user Quadlet and start its generated service.
    install -d -m 700 "${QUADLET_DIR}"
    install -m 644 "${SCRIPT_DIR}/${UNIT}" "${QUADLET_DEST}"
    systemctl --user daemon-reload
    systemctl --user start "${SERVICE}"
}

status() {
    # Show only the generated prototype service state.
    systemctl --user status "${SERVICE}"
}

logs() {
    # Follow only the generated prototype service journal.
    journalctl --user --unit "${SERVICE}" --follow
}

down() {
    # Stop and disable the prototype service if it is currently known to systemd.
    systemctl --user disable --now "${SERVICE}" 2>/dev/null || true

    # Remove only this Quadlet definition and preserve all prototype evidence.
    rm -f -- "${QUADLET_DEST}"
    systemctl --user daemon-reload
}

# Dispatch the requested lifecycle action without accepting extra arguments.
if [[ $# -ne 1 ]]; then
    usage >&2
    exit 2
fi

# Run exactly one supported lifecycle action.
case "$1" in
    up)
        up
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    down)
        down
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
