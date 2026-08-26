"""Tests for the standalone container release policy module."""

import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "container-release.py"
SPEC = importlib.util.spec_from_file_location("container_release", SCRIPT)
assert SPEC is not None
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _raises(function: Callable[..., object], *args: object) -> None:
    # Keep malformed-input checks concise without adding a test dependency.
    try:
        function(*args)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_release_tags_and_malformed_inputs() -> None:
    # Accept valid release tags and reject every unsupported SemVer extension.
    assert str(release.parse_release_tag("v1.2.3")) == "1.2.3"
    assert str(release.parse_release_tag("v1.2.3-rc.1")) == "1.2.3-rc.1"
    for tag in ("1.2.3", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2", "v1.2.3+build", "v1.2.3-01", "v1.2.3-", "v1.2.3-α", "v1.2.3\nkey=x", "v" + "1" * 257):
        _raises(release.parse_release_tag, tag)


def test_semver_prerelease_precedence() -> None:
    # Check the SemVer specification ordering sequence and stable-release precedence.
    ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
    versions = [release.parse_semver(value) for value in ordered]
    assert all(release.compare_semver(left, right) < 0 for left, right in zip(versions, versions[1:]))


def test_revision_and_digest_validation() -> None:
    # Normalize accepted full revisions and reject noncanonical digest forms.
    assert release.normalize_revision("A" * 40) == "a" * 40
    for revision in ("a" * 39, "g" * 40, "a" * 40 + "\n"):
        _raises(release.normalize_revision, revision)
    assert release.validate_digest(DIGEST_A) == DIGEST_A
    for digest in ("sha256:" + "A" * 64, "sha256:" + "a" * 63, "sha512:" + "a" * 64):
        _raises(release.validate_digest, digest)


def test_immutable_policy_states() -> None:
    # Cover each immutable tag state, including both repair directions.
    assert release.immutable_policy(None, None).action == "publish"
    assert release.immutable_policy(DIGEST_A, DIGEST_A).action == "noop"
    assert release.immutable_policy(DIGEST_A, DIGEST_B).action == "conflict"
    assert release.immutable_policy(None, DIGEST_A) == release.ImmutableDecision("repair-version", DIGEST_A)
    assert release.immutable_policy(DIGEST_A, None) == release.ImmutableDecision("repair-sha", DIGEST_A)


def test_latest_policy_states() -> None:
    # Cover prerelease, absent, older, newer, equal, and conflicting latest states.
    stable = release.parse_semver("1.2.3")
    assert release.latest_policy(release.parse_semver("1.2.4-rc.1"), DIGEST_A, None, None).action == "skip"
    assert release.latest_policy(stable, DIGEST_A, None, None).action == "move"
    assert release.latest_policy(stable, DIGEST_A, release.parse_semver("1.2.2"), DIGEST_B).action == "move"
    assert release.latest_policy(stable, DIGEST_A, release.parse_semver("1.2.4"), DIGEST_B).action == "skip"
    assert release.latest_policy(stable, DIGEST_A, stable, DIGEST_A).action == "noop"
    assert release.latest_policy(stable, DIGEST_A, stable, DIGEST_B).action == "conflict"

    # Reject incomplete digest and latest-alias inputs before applying any policy state.
    _raises(release.latest_policy, stable, "", None, None)
    _raises(release.latest_policy, stable, DIGEST_A, stable, None)
    _raises(release.latest_policy, stable, DIGEST_A, None, DIGEST_A)


def test_latest_command_requires_consistent_alias_pair() -> None:
    """Check CLI handling for an absent or incomplete latest alias pair."""
    # Accept a fully absent latest pair and write the resulting action for workflows.
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        arguments = ["latest", "--candidate-version", "1.2.3", "--candidate-digest", DIGEST_A]
        assert release.main([*arguments, "--github-output", str(output)]) == 0
        assert output.read_text() == "action=move\n"

        # Reject a lone latest version instead of treating it as an absent pair.
        assert release.main([*arguments, "--latest-version", "1.2.2", "--github-output", str(output)]) == 2


def test_validate_command_and_output_safety() -> None:
    # Validate matching source versions and confirm only normalized trusted output is written.
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        result = release.main(["validate", "--tag", "v0.4.0", "--revision", "A" * 40, "--project-root", str(ROOT), "--github-output", str(output)])
        assert result == 0
        assert output.read_text() == f"version=0.4.0\nversion_tag=0.4.0\nsha_tag=sha-{'a' * 40}\nrevision={'a' * 40}\nprerelease=false\nsource={release.SOURCE}\n"
        assert "version_tag=v" not in output.read_text()
        link = Path(directory) / "link"
        link.symlink_to(output)
        assert release.main(["validate", "--tag", "v0.4.0", "--revision", "a" * 40, "--project-root", str(ROOT), "--github-output", str(link)]) == 2


def test_source_version_mismatch_and_bounds_redaction() -> None:
    # Reject mismatched static declarations and avoid reflecting hostile input in errors.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "meeting_recorder").mkdir()
        (root / "meeting_recorder" / "__init__.py").write_text('__version__ = "1.0.0"\n')
        (root / "pyproject.toml").write_text('[project]\nversion = "1.0.1"\n')
        _raises(release.validate_source_versions, root, release.parse_semver("1.0.0"))
        assert release.main(["validate", "--tag", "v1.0.0\nsecret", "--revision", "a" * 40, "--project-root", str(root), "--github-output", str(root / "out")]) == 2


def test_containerfile_uses_unknown_fallback() -> None:
    # Keep ordinary builds visibly separate from authoritative release builds.
    assert 'ARG OCI_VERSION="unknown"' in (ROOT / "Containerfile").read_text()
