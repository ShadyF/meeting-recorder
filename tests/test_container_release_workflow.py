"""Static checks for the isolated GHCR container release workflow."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/container-release.yml"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_trigger_permissions_concurrency_and_pins() -> None:
    text = workflow_text()

    # Restrict this workflow to tag publication with its minimal job permissions.
    assert 'tags:\n      - "v*"' in text
    assert "workflow_dispatch" not in text and "schedule:" not in text
    assert "packages: write" in text
    assert "group: ghcr-meeting-recorder-publication" in text
    assert "cancel-in-progress: false" in text

    # Keep every third-party action on its reviewed immutable commit.
    for pin in (
        "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6.1.0",
        "docker/setup-buildx-action@37fe631027851001ddb9b187196cc803df7f5f0e # v4.3.0",
        "docker/login-action@dbcb813823bdd20940b903addbd779551569679f # v4.6.0",
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a # v7.3.0",
    ):
        assert pin in text
    assert "persist-credentials: false" in text


def test_registry_policy_build_and_digest_flow() -> None:
    text = workflow_text()

    # Run tests before the login step that grants package publication access.
    assert text.index("python tests/run_tests.py") < text.index("docker/login-action")
    assert "python3 scripts/container-release.py validate" in text
    assert "python3 scripts/container-release.py immutable" in text
    assert "python3 scripts/container-release.py latest" in text
    assert "python3 scripts/container-registry.py inspect" in text
    assert "python3 scripts/container-registry.py alias" in text
    assert "python3 scripts/container-registry.py move" in text
    assert "python3 scripts/container-registry.py verify" in text
    assert "\n          scripts/container-release.py" not in text
    assert "\n          scripts/container-registry.py" not in text
    assert "registry-plan" not in text and "registry-apply" not in text and "registry-finalize" not in text

    # Exercise each immutable action and publish only bare SemVer and full-SHA tags.
    for action in ("publish", "repair-version", "repair-sha", "noop", "conflict"):
        assert action in text
    assert "ghcr.io/shadyf/meeting-recorder:${{ needs.validate.outputs.version_tag }}" in text
    assert "ghcr.io/shadyf/meeting-recorder:${{ needs.validate.outputs.sha_tag }}" in text
    assert "ghcr.io/shadyf/meeting-recorder:v${{" not in text
    assert "platforms: linux/amd64" in text and "provenance: mode=max" in text and "sbom: true" in text
    assert "type=gha" not in text
    assert "--existing-image \"$LOCAL_IMAGE\" --expected-version" in text
    assert "--expected-revision \"$REVISION\" --expected-source \"$SOURCE\"" in text

    # Verify immutable tags before anonymous acceptance and defer mutable latest handling.
    assert text.index("Verify immutable release tags") < text.index("anonymous-smoke:")
    assert "promote-latest:\n    needs: [validate, publish, anonymous-smoke]" in text
    assert text.index("anonymous-smoke:") < text.index("promote-latest:")
    promote_latest = text.split("  promote-latest:\n", 1)[1]
    publish = text.split("  publish:\n", 1)[1].split("  anonymous-smoke:\n", 1)[0]
    assert "--target-tag latest" not in publish
    assert "--target-tag latest --expected-version" in promote_latest
    assert "container-registry.py move --image \"$IMAGE\"" in promote_latest
    assert "container-registry.py alias --image \"$IMAGE\" --source-digest \"$DIGEST\" --target-tag latest" not in promote_latest
    assert "Inspect the latest tag after anonymous acceptance" in promote_latest
    assert "needs.publish.outputs.digest" in promote_latest
    assert "--target-tag latest --expected-version" in text
    assert "id: release" in text and "digest: ${{ steps.release.outputs.digest }}" in text


def test_anonymous_smoke_is_isolated_and_hardened() -> None:
    text = workflow_text()

    # Check out only the tagged acceptance script without restoring registry credentials.
    assert "needs: [validate, publish]" in text
    assert "contents: read" in text
    assert text.count("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6.1.0") == 4

    # Isolate anonymous pulls from inherited credentials and retry transient visibility delays.
    assert 'chmod 700 "$DOCKER_CONFIG"' in text
    assert "unset DOCKER_AUTH_CONFIG REGISTRY_AUTH_FILE" in text
    assert "for delay in 5 10 20 40 80" in text
    assert 'docker pull "$IMAGE@$DIGEST"' in text

    # Run complete hardened acceptance only against the exact pulled digest.
    assert 'scripts/test-runtime-image.sh --existing-image "$IMAGE@$DIGEST"' in text
    assert '--expected-version "$VERSION" --expected-revision "$REVISION" --expected-source "$SOURCE"' in text

    # Keep package, deployment, and automatic-update paths out of the image workflow.
    for forbidden in ("debian", "gpg", "gh-pages", "pull_request", "metadata-action", "attest-build-provenance", "deployment"):
        assert forbidden not in text.lower()
