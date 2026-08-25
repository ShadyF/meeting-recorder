"""Static checks for the generic GitHub release workflows."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"


def test_debian_and_apt_automation_is_absent() -> None:
    """Keep removed Debian package publication paths from returning."""
    ci_text = CI_WORKFLOW.read_text(encoding="utf-8").lower()
    release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8").lower()

    # Reject package build, APT, and source-package automation in active workflows.
    for forbidden in (
        "apt-get",
        "apt-repo",
        "build-deb",
        "debian/",
        "dpkg-buildpackage",
        "lintian",
        "*.deb",
        "release-footer",
    ):
        assert forbidden not in ci_text
        assert forbidden not in release_text

    # Require the obsolete reusable workflow and release-note footer to stay deleted.
    assert not (ROOT / ".github/workflows/apt-repo.yml").exists()
    assert not (ROOT / ".github/release-footer.md").exists()


def test_tag_release_uses_strict_validation_and_generic_release_creation() -> None:
    """Keep the source release independent from container publication."""
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    # Restrict CI and release jobs to their separate minimum token scopes.
    assert 'tags:\n      - "v*"' in text
    assert "contents: write" in text
    assert "packages: write" not in text
    assert "permissions:\n  contents: read" in CI_WORKFLOW.read_text(encoding="utf-8")

    # Check out without persisted credentials and validate both static source versions.
    assert "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6.1.0" in text
    assert "persist-credentials: false" in text
    assert "scripts/container-release.py validate" in text
    assert "--tag \"$RELEASE_TAG\"" in text
    assert "id: validation" in text

    # Publish changelog notes as a GitHub Release without release assets.
    assert "CHANGELOG.md > notes.md" in text
    assert "gh release create \"$GITHUB_REF_NAME\"" in text
    assert "--notes-file notes.md" in text

    # Derive prerelease state only from the strict validator output.
    assert "RELEASE_PRERELEASE: ${{ steps.validation.outputs.prerelease }}" in text
    assert '[[ "$RELEASE_PRERELEASE" == "true" ]]' in text
    assert "release_args+=(--prerelease --latest=false)" in text
    assert "release_args+=(--prerelease=false)" in text

    # Create missing releases and update existing releases with the same trusted flags.
    assert 'if gh release view "$GITHUB_REF_NAME" >/dev/null 2>&1; then' in text
    assert 'gh release edit "$GITHUB_REF_NAME" "${release_args[@]}"' in text
    assert 'gh release create "$GITHUB_REF_NAME" --verify-tag "${release_args[@]}"' in text
