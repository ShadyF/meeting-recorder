#!/usr/bin/env bash
# Accept the local runtime image without using host desktop resources.
set -euo pipefail

# Print one short status line for each acceptance stage.
step() {
    printf '==> %s\n' "$1"
}

# Stop immediately with a useful message when the script contract is not met.
die() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

# Resolve the repository from this script so invocation from another directory is safe.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if ! ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null)"; then
    die "could not resolve the repository root"
fi
ROOT="$(cd -- "$ROOT" && pwd -P)"
[[ -f "$ROOT/Containerfile" ]] || die "Containerfile is missing at repository root: $ROOT"

# Parse the deliberately small command-line surface and keep the default tag project-local.
IMAGE_TAG="${IMAGE_TAG:-meeting-recorder:runtime-acceptance}"
KEEP_IMAGE="${KEEP_IMAGE:-0}"
POSITIONAL_TAG=0
EXISTING_IMAGE="${EXISTING_IMAGE:-}"
EXPECTED_VERSION="${EXPECTED_OCI_VERSION:-}"
EXPECTED_REVISION="${EXPECTED_OCI_REVISION:-}"
EXPECTED_SOURCE="${EXPECTED_OCI_SOURCE:-}"
while (($#)); do
    case "$1" in
        --existing-image)
            shift
            (($#)) || die "--existing-image requires an image reference"
            [[ -z "$EXISTING_IMAGE" ]] || die "existing image was supplied more than once"
            EXISTING_IMAGE="$1"
            ;;
        --expected-version)
            shift
            (($#)) || die "--expected-version requires a value"
            EXPECTED_VERSION="$1"
            ;;
        --expected-revision)
            shift
            (($#)) || die "--expected-revision requires a value"
            EXPECTED_REVISION="$1"
            ;;
        --expected-source)
            shift
            (($#)) || die "--expected-source requires a value"
            EXPECTED_SOURCE="$1"
            ;;
        --keep-image)
            KEEP_IMAGE=1
            ;;
        --help|-h)
            printf 'Usage: %s [--keep-image] [IMAGE_TAG]\n' "${BASH_SOURCE[0]}"
            printf '       %s --existing-image IMAGE --expected-version VERSION --expected-revision REVISION --expected-source SOURCE\n' "${BASH_SOURCE[0]}"
            printf 'Environment: DOCKER_BIN, IMAGE_TAG, KEEP_IMAGE, RUNTIME_IMAGE_UID_GID\n'
            printf '             EXISTING_IMAGE, EXPECTED_OCI_VERSION, EXPECTED_OCI_REVISION, EXPECTED_OCI_SOURCE\n'
            exit 0
            ;;
        --)
            shift
            (($# == 1)) || die "expected at most one image tag"
            ((POSITIONAL_TAG == 0)) || die "image tag was supplied more than once"
            IMAGE_TAG="$1"
            POSITIONAL_TAG=1
            ;;
        -* )
            die "unknown option: $1"
            ;;
        *)
            ((POSITIONAL_TAG == 0)) || die "image tag was supplied more than once"
            IMAGE_TAG="$1"
            POSITIONAL_TAG=1
            ;;
    esac
    shift
done
[[ -n "$IMAGE_TAG" && "$IMAGE_TAG" != *[[:space:]]* ]] || die "image tag must be non-empty and contain no whitespace"

# Keep existing-image inputs bounded before they are passed to Docker or the metadata check.
if [[ -n "$EXISTING_IMAGE" ]]; then
    [[ "$POSITIONAL_TAG" == 0 ]] || die "IMAGE_TAG cannot be used with --existing-image"

    # Reject local-build cleanup controls because existing images always remain caller-owned.
    [[ "$KEEP_IMAGE" != 1 ]] || die "--keep-image and KEEP_IMAGE=1 cannot be used with --existing-image"

    [[ "$EXISTING_IMAGE" != *[[:space:]]* && ${#EXISTING_IMAGE} -le 512 ]] \
        || die "existing image must contain no whitespace and be at most 512 characters"
    [[ -n "$EXPECTED_VERSION" && "$EXPECTED_VERSION" != *[[:space:]]* && ${#EXPECTED_VERSION} -le 256 ]] \
        || die "existing-image mode requires a non-empty expected version of at most 256 non-whitespace characters"
    [[ "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]] \
        || die "existing-image mode requires an expected full 40-character lowercase hexadecimal revision"
    [[ -n "$EXPECTED_SOURCE" && "$EXPECTED_SOURCE" != *[[:space:]]* && ${#EXPECTED_SOURCE} -le 2048 ]] \
        || die "existing-image mode requires a non-empty expected source of at most 2048 non-whitespace characters"
elif [[ -n "$EXPECTED_VERSION$EXPECTED_REVISION$EXPECTED_SOURCE" ]]; then
    die "expected OCI metadata options are only valid with --existing-image"
fi

# Use a numeric identity so every filesystem assertion exercises the non-root path.
DOCKER_BIN="${DOCKER_BIN:-docker}"
RUN_UID_GID="${RUNTIME_IMAGE_UID_GID:-65532:65532}"
[[ "$RUN_UID_GID" =~ ^[0-9]+:[0-9]+$ ]] || die "RUNTIME_IMAGE_UID_GID must be numeric UID:GID"
command -v "$DOCKER_BIN" >/dev/null 2>&1 || die "Docker executable not found: $DOCKER_BIN"

# Read build metadata locally or use the caller-supplied contract for an existing image.
if [[ -n "$EXISTING_IMAGE" ]]; then
    IMAGE_REF="$EXISTING_IMAGE"
    VERSION="$EXPECTED_VERSION"
    REVISION="$EXPECTED_REVISION"
    SOURCE="$EXPECTED_SOURCE"
else
    # Read the exact revision used by the local build.
    if ! REVISION="$(git -C "$ROOT" rev-parse HEAD)"; then
        die "could not read the git revision"
    fi

    # Compare both package declarations so the build label uses the authoritative release version.
    if ! VERSION="$(python3 - "$ROOT/meeting_recorder/__init__.py" "$ROOT/pyproject.toml" <<'PY'
import ast
import sys
import tomllib

init_path, pyproject_path = sys.argv[1:]

# Read the package version without importing project code.
with open(init_path, encoding="utf-8") as stream:
    module = ast.parse(stream.read(), filename=init_path)

# Locate the literal package version declaration.
init_version = None
for statement in module.body:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "__version__"
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    ):
        init_version = statement.value.value
        break

# Read the project version from the standard TOML metadata.
with open(pyproject_path, "rb") as stream:
    project_version = tomllib.load(stream).get("project", {}).get("version")

# Reject missing or divergent declarations before passing metadata to the build.
if not init_version or not isinstance(project_version, str) or init_version != project_version:
    raise SystemExit("package and project versions must be non-empty and identical")

print(init_version)
PY
    )"; then
        die "could not derive the authoritative package version"
    fi

    # Use the public source URL defined by the image contract.
    SOURCE="https://github.com/ShadyF/meeting-recorder"
    IMAGE_REF="$IMAGE_TAG"
fi

# Validate every metadata value before it is passed to the build or comparison command.
[[ -n "$VERSION" && "$VERSION" != *[[:space:]]* && ${#VERSION} -le 256 ]] \
    || die "OCI version must be non-empty and at most 256 non-whitespace characters"
[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] \
    || die "OCI revision must be a full 40-character lowercase hexadecimal revision"
[[ -n "$SOURCE" && "$SOURCE" != *[[:space:]]* && ${#SOURCE} -le 2048 ]] \
    || die "OCI source must be non-empty and at most 2048 non-whitespace characters"

# Keep a temporary host-side file for jq-free image inspection and clean it on every exit.
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/meeting-recorder-runtime.XXXXXXXX")"
BUILT_IMAGE_ID=""

# Remove only the captured image ID; never run global Docker cleanup.
cleanup() {
    # Preserve the acceptance result while cleanup runs with explicit error handling.
    final_status=$?

    # Keep cleanup failures visible instead of allowing the EXIT trap to stop early.
    set +e

    if [[ "$KEEP_IMAGE" != 1 && -n "$BUILT_IMAGE_ID" ]]; then
        # Remove by immutable ID so a concurrent retag cannot redirect cleanup.
        "$DOCKER_BIN" image rm "$BUILT_IMAGE_ID" >/dev/null 2>&1
        remove_status=$?

        # Docker refuses removal when unrelated references still protect the image.
        if ((remove_status != 0)); then
            printf 'ERROR: could not remove disposable image ID %s\n' "$BUILT_IMAGE_ID" >&2
            final_status=1
        fi
    elif [[ "$KEEP_IMAGE" == 1 && -z "$EXISTING_IMAGE" ]]; then
        # Preserve the image when the caller requests post-run inspection.
        printf 'Keeping image: %s\n' "$IMAGE_TAG"
    fi

    # Remove only this script's temporary inspection data.
    rm -rf -- "$TEMP_DIR"

    # Return the original result unless cleanup itself made the run unsafe.
    exit "$final_status"
}
trap cleanup EXIT

# Build a disposable local image, or leave the caller-owned existing image untouched.
if [[ -z "$EXISTING_IMAGE" ]]; then
    # Use the repository as the build working directory so the Dockerfile argument is exact.
    cd -- "$ROOT"

    # Build one platform image and load it into the local Docker image store.
    step "Building $IMAGE_TAG"
    "$DOCKER_BIN" buildx build \
        --platform linux/amd64 \
        --load \
        -f Containerfile \
        --tag "$IMAGE_TAG" \
        --build-arg "OCI_VERSION=$VERSION" \
        --build-arg "OCI_REVISION=$REVISION" \
        --build-arg "OCI_SOURCE=$SOURCE" \
        "$ROOT"

    # Capture the immutable image identity for safe cleanup after all probes finish.
    BUILT_IMAGE_ID="$("$DOCKER_BIN" image inspect --format '{{.Id}}' "$IMAGE_REF")"
else
    # Report the local caller-owned image without pulling, logging in, or changing its tags.
    step "Using existing image"
fi

# Inspect the loaded image with host Python and assert the complete runtime contract.
step "Inspecting image metadata"
"$DOCKER_BIN" image inspect "$IMAGE_REF" >"$TEMP_DIR/image-inspect.json"
python3 - "$TEMP_DIR/image-inspect.json" "$VERSION" "$REVISION" "$SOURCE" <<'PY'
import json
import sys

inspect_path, expected_version, expected_revision, expected_source = sys.argv[1:]
# Read the single image record produced by docker image inspect.
with open(inspect_path, encoding="utf-8") as stream:
    document = json.load(stream)

# Reject an unexpectedly empty inspect response before reading image fields.
if not document:
    raise SystemExit("image inspect returned no image")

# Select the single image record returned by Docker.
image = document[0]

# Read the image configuration fields used by the contract checks.
config = image.get("Config") or {}

# Convert Docker's environment list into values that can be compared directly.
env = {}
for item in config.get("Env") or []:
    name, separator, value = item.partition("=")

    # Ignore malformed environment entries instead of inventing a value.
    if separator:
        env[name] = value

# Read OCI labels from the image configuration for validation.
labels = config.get("Labels") or {}

# Compare each contract field and report the first mismatch concisely.
checks = {
    "OS": (image.get("Os"), "linux"),
    "architecture": (image.get("Architecture"), "amd64"),
    "working directory": (config.get("WorkingDir"), "/opt/meeting-recorder"),
    "entrypoint": (config.get("Entrypoint"), ["/usr/local/bin/meeting-recorder"]),
    "command": (config.get("Cmd"), ["run"]),
    "configured user": (config.get("User") or "", ""),
    "XDG_CONFIG_HOME": (env.get("XDG_CONFIG_HOME"), "/config"),
    "XDG_STATE_HOME": (env.get("XDG_STATE_HOME"), "/state"),
    "XDG_CACHE_HOME": (env.get("XDG_CACHE_HOME"), "/cache"),
    "OCI title label": (labels.get("org.opencontainers.image.title"), "Smart Meeting Recorder"),
    "OCI version label": (labels.get("org.opencontainers.image.version"), expected_version),
    "OCI revision label": (labels.get("org.opencontainers.image.revision"), expected_revision),
    "OCI source label": (labels.get("org.opencontainers.image.source"), expected_source),
    "OCI license label": (labels.get("org.opencontainers.image.licenses"), "MIT"),
}

# Stop at the first contract mismatch without exposing supplied metadata.
for name, (actual, expected) in checks.items():
    if actual != expected:
        raise SystemExit(f"{name}: image metadata did not match the required value")
PY

# Build common hardened flags once so every runtime probe has the same restrictions.
HARDENED_RUN_FLAGS=(
    --network=none
    --read-only
    --cap-drop=ALL
    --security-opt=no-new-privileges
    --user "$RUN_UID_GID"
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777
    --env HOME=/tmp/home
    --env XDG_CONFIG_HOME=/tmp/xdg/config
    --env XDG_STATE_HOME=/tmp/xdg/state
    --env XDG_CACHE_HOME=/tmp/xdg/cache
    --env XDG_DATA_HOME=/tmp/xdg/data
    --env XDG_RUNTIME_DIR=/tmp/xdg/runtime
)

# Run the installed launcher with the hardened runtime flags and temporary XDG roots.
run_app() {
    "$DOCKER_BIN" run --rm "${HARDENED_RUN_FLAGS[@]}" "$IMAGE_REF" "$@"
}

# Run a shell-only filesystem or dependency probe without invoking the launcher preflight.
run_shell() {
    "$DOCKER_BIN" run --rm "${HARDENED_RUN_FLAGS[@]}" --entrypoint /bin/sh "$IMAGE_REF" -c "$1"
}

# Verify the installed Python package, its defaults, and every required GI namespace.
step "Checking Python and GI imports"
run_shell "python3 -c 'import meeting_recorder; from meeting_recorder.config import load_config, load_defaults; assert isinstance(load_defaults(), dict); load_config()'"
run_shell "python3 -c 'import gi; [gi.require_version(name, version) for name, version in ((\"Gtk\", \"3.0\"), (\"Notify\", \"0.7\"), (\"AppIndicator3\", \"0.1\"), (\"Secret\", \"1\"), (\"Gio\", \"2.0\"), (\"GLib\", \"2.0\"))]; from gi.repository import Gtk, Notify, AppIndicator3, Secret, Gio, GLib; import cairo'"

# Check command-line tools without starting any desktop or audio service.
step "Checking required commands"
run_shell 'set -eu
for command_name in python3 ffmpeg ffprobe pactl gst-launch-1.0 gst-inspect-1.0 notify-send secret-tool xdg-open xrandr xprop xwininfo; do
    command -v "$command_name" >/dev/null 2>&1
done'

# Check the GStreamer elements used by both capture paths.
step "Checking GStreamer elements"
run_shell 'set -eu
for element in pipewiresrc videorate videocrop videoconvert videoscale filesink; do
    gst-inspect-1.0 "$element" >/dev/null
done'

# Check that package-installed desktop integration files are present and non-empty.
step "Checking desktop entries and icons"
run_shell 'set -eu
for path in \
    /usr/share/applications/meeting-recorder.desktop \
    /usr/share/applications/meeting-recorder-settings.desktop \
    /usr/share/icons/hicolor/scalable/apps/meeting-recorder.svg \
    /usr/share/icons/hicolor/scalable/apps/meeting-recorder-recording.svg \
    /usr/share/icons/hicolor/scalable/apps/meeting-recorder-paused.svg; do
    test -s "$path"
done'

# Prove the immutable application tree rejects writes while the temporary filesystem accepts them.
step "Checking hardened filesystem"
run_shell 'set -eu
test -d /opt/meeting-recorder
test ! -w /opt/meeting-recorder
test -w /tmp
touch /tmp/runtime-image-write-test
rm -f /tmp/runtime-image-write-test'

# Check the launcher version path before exercising commands that write temporary state.
step "Checking launcher version"
run_app --version

# Exercise admin commands with empty writable state; these commands must bypass desktop preflight.
step "Checking writable admin commands"

# Create the user configuration only in the temporary XDG root.
config_output="$(run_app config)"
printf '%s\n' "$config_output"

# Confirm the config command used the writable smoke-test location.
[[ "$config_output" == "User config: /tmp/xdg/config/meeting-recorder/config.json" ]] \
    || die "config did not use the temporary XDG config root"

# Preview cleanup against the empty publication state without deleting anything.
cleanup_output="$(run_app cleanup --older-than 1)"
printf '%s\n' "$cleanup_output"

# Validate the bounded empty cleanup report with host Python.
python3 -c 'import json, sys; value = json.load(sys.stdin); assert value["command"] == "cleanup"; assert value["mode"] == "preview"; assert value["older_than_days"] == 1; assert value["results"] == []' <<<"$cleanup_output"

# Exercise nested parser help without contacting Calendar or Speakr services.
step "Checking Calendar and Speakr parsers"
run_app calendar --help >/dev/null
run_app calendar select --help >/dev/null
run_app speakr --help >/dev/null
run_app speakr upload --help >/dev/null

# Confirm the ordinary default command is rejected visibly when no desktop resources exist.
step "Checking headless run preflight"

# Capture the expected non-zero admission result without stopping the acceptance script.
set +e
preflight_output="$(run_app run 2>&1)"
preflight_status=$?
set -e

# Preserve the launcher message so a missing-resource failure remains visible.
printf '%s\n' "$preflight_output"

# Require both a failure status and a resource-related explanation.
((preflight_status != 0)) || die "default run unexpectedly passed without desktop resources"
[[ -n "$preflight_output" ]] || die "default run preflight failure was not visible"
printf '%s\n' "$preflight_output" | grep -Eiq 'preflight|desktop|display|wayland|pulse|pipewire|d.bus|resource' \
    || die "default run failure did not identify missing desktop resources"

step "Runtime image acceptance passed"
