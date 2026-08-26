"""Focused tests for the bounded Buildx registry adapter."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


# Load the hyphenated script as a normal test module without changing sys.path.
SCRIPT = Path(__file__).parents[1] / "scripts" / "container-registry.py"
SPEC = importlib.util.spec_from_file_location("container_registry", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
registry_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = registry_module
SPEC.loader.exec_module(registry_module)

DIGEST = "sha256:" + "a" * 64
CHILD = "sha256:" + "b" * 64
IMAGE = "ghcr.io/acme/image"
LABELS = {
    "org.opencontainers.image.version": "1.2.3",
    "org.opencontainers.image.revision": "c" * 40,
    "org.opencontainers.image.source": "https://example.test/repo",
}


class FakeRunner:
    """Return queued subprocess outcomes and retain Buildx argument vectors."""

    def __init__(self, replies: Sequence[object]) -> None:
        self.replies = list(replies)
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str], timeout: float) -> registry_module.RunResult:
        self.calls.append(list(arguments))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        assert isinstance(reply, registry_module.RunResult)
        return reply


def result(stdout: str = "", code: int = 0, stderr: str = "") -> registry_module.RunResult:
    """Construct a small fake subprocess result."""
    return registry_module.RunResult(code, stdout, stderr)


def direct_manifest() -> str:
    """Return an OCI image manifest used for direct-reference tests."""
    return '{"mediaType":"application/vnd.oci.image.manifest.v1+json"}'


def image_metadata(labels: dict[str, object] | None = None, os_name: str = "linux", architecture: str = "amd64") -> str:
    """Return the live Buildx .Image JSON shape used by the adapter."""
    return json.dumps({"os": os_name, "architecture": architecture, "config": {"Labels": LABELS if labels is None else labels}})


def index_manifest(children: list[dict[str, object]]) -> str:
    """Return a bounded OCI index containing supplied child descriptors."""
    return json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": children})


def inspection_replies(raw: str = direct_manifest(), digest: str = DIGEST, metadata: str | None = None) -> list[registry_module.RunResult]:
    """Build the three Buildx responses required for one successful inspection."""
    return [result(raw), result(digest + "\n"), result(image_metadata() if metadata is None else metadata)]


def inspect(fake: FakeRunner) -> registry_module.Inspection:
    """Inspect the standard test reference through the public adapter method."""
    return registry_module.Registry(fake).inspect(IMAGE, "v1")


def test_absent_reference_emits_false_without_failure() -> None:
    """A confirmed absent tag is a successful query result."""
    # Run the CLI path to ensure only a classified absence produces present=false.
    fake = FakeRunner([result(code=1, stderr="manifest unknown")])
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "out"
        assert registry_module.main(["inspect", "--image", IMAGE, "--tag", "v1", "--github-output", str(output)], registry_module.Registry(fake)) == 0
        assert output.read_text() == "present=false\n"


def test_direct_manifest_requires_linux_amd64_image_metadata() -> None:
    """A direct manifest is accepted only after Buildx proves its platform."""
    # Accept the proven linux/amd64 image and inspect exactly the .Image template.
    fake = FakeRunner(inspection_replies())
    assert inspect(fake).digest == DIGEST
    assert fake.calls[-1] == ["docker", "buildx", "imagetools", "inspect", f"{IMAGE}:v1", "--format", "{{json .Image}}"]

    # Reject a direct image that has a non-amd64 platform despite a valid manifest.
    non_amd64 = FakeRunner(inspection_replies(metadata=image_metadata(architecture="arm64")))
    try:
        inspect(non_amd64)
    except registry_module.RegistryError:
        pass
    else:
        raise AssertionError("accepted an arm64 direct image")


def test_index_accepts_one_amd64_image_and_buildkit_attestation() -> None:
    """An index may contain one runnable image plus recognized attestations only."""
    # Give the index its own digest and read labels from the selected child digest.
    child = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": CHILD, "platform": {"os": "linux", "architecture": "amd64"}}
    attestation = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": DIGEST, "platform": {"os": "unknown", "architecture": "unknown"}, "annotations": {"vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": CHILD}}
    fake = FakeRunner(inspection_replies(index_manifest([attestation, child])))
    assert inspect(fake).digest == DIGEST
    assert fake.calls[-1][4] == f"{IMAGE}@{CHILD}"


def test_index_requires_bound_attestations_and_rejects_other_children() -> None:
    """Indexes require attestations bound to their only runnable child."""
    # Exercise missing attestations, invalid bindings, and an extra runnable architecture.
    amd64 = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": CHILD, "platform": {"os": "linux", "architecture": "amd64"}}
    arm64 = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": DIGEST, "platform": {"os": "linux", "architecture": "arm64"}}
    unknown = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": DIGEST, "platform": {"os": "unknown", "architecture": "unknown"}}
    valid_attestation = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": DIGEST, "platform": {"os": "unknown", "architecture": "unknown"}, "annotations": {"vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": CHILD}}
    wrong_subject = {**valid_attestation, "annotations": {"vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": DIGEST}}
    missing_subject = {**valid_attestation, "annotations": {"vnd.docker.reference.type": "attestation-manifest"}}
    malformed_subject = {**valid_attestation, "annotations": {"vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": "sha256:" + "A" * 64}}
    for children in ([], [amd64], [amd64, arm64, valid_attestation], [amd64, unknown], [amd64, wrong_subject], [amd64, missing_subject], [amd64, malformed_subject]):
        try:
            inspect(FakeRunner([result(index_manifest(children))]))
        except registry_module.RegistryError:
            pass
        else:
            raise AssertionError("accepted an invalid index child set")


def test_malformed_oversized_digest_and_label_responses_are_rejected() -> None:
    """Malformed registry data cannot become a release identity."""
    # Cover decode bounds, canonical digest validation, and invalid config label types.
    cases = [
        [result("{")],
        [result("x" * (registry_module.MAX_JSON + 1))],
        [result(direct_manifest()), result("bad")],
        inspection_replies(metadata=image_metadata({"org.opencontainers.image.version": 3})),
    ]
    for queued in cases:
        try:
            inspect(FakeRunner(queued))
        except registry_module.RegistryError:
            pass
        else:
            raise AssertionError("accepted malformed registry metadata")


def test_timeout_and_registry_errors_are_redacted_by_cli() -> None:
    """The CLI hides timeout and registry secrets while exercising adapter handling."""
    # Invoke main so stderr proves the public error boundary does not leak fake secrets.
    failures: list[object] = [
        subprocess.TimeoutExpired(["docker"], 20, stderr="timeout-secret"),
        result(code=1, stderr="network secret-token"),
        result(code=1, stderr="denied secret-token"),
    ]
    for failure in failures:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(stderr):
            output = Path(directory) / "out"
            code = registry_module.main(["inspect", "--image", IMAGE, "--tag", "v1", "--github-output", str(output)], registry_module.Registry(FakeRunner([failure])))
        assert code == 2
        assert stderr.getvalue() == "container registry operation failed\n"
        assert "secret" not in stderr.getvalue()


def alias_args(output: Path) -> list[str]:
    """Return a complete alias command with trusted expected labels."""
    return ["alias", "--image", IMAGE, "--source-digest", DIGEST, "--target-tag", "v1", "--expected-version", "1.2.3", "--expected-revision", "c" * 40, "--expected-source", "https://example.test/repo", "--github-output", str(output)]


def move_args(output: Path) -> list[str]:
    """Return a complete mutable-tag move command with trusted expected labels."""
    return ["move", "--image", IMAGE, "--source-digest", DIGEST, "--target-tag", "latest", "--expected-version", "1.2.3", "--expected-revision", "c" * 40, "--expected-source", "https://example.test/repo", "--github-output", str(output)]


def test_alias_creates_absent_target_and_postverifies() -> None:
    """A missing alias is created once and then read back exactly."""
    # Queue source inspection, target absence, create, and final target inspection.
    fake = FakeRunner(inspection_replies() + [result(code=1, stderr="manifest unknown"), result()] + inspection_replies())
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "out"
        assert registry_module.main(alias_args(output), registry_module.Registry(fake)) == 0
        assert output.read_text().endswith(f"digest={DIGEST}\naction=created\n")
    assert any(call[3] == "create" for call in fake.calls)


def test_alias_same_target_is_a_noop_without_create() -> None:
    """An existing matching alias is verified and never mutated."""
    # Queue source, immediate target, and post-check inspections with no create reply.
    fake = FakeRunner(inspection_replies() + inspection_replies() + inspection_replies())
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "out"
        assert registry_module.main(alias_args(output), registry_module.Registry(fake)) == 0
        assert output.read_text().endswith("action=noop\n")
    assert not any(call[3] == "create" for call in fake.calls)


def test_alias_rejects_target_conflict_and_source_label_mismatch() -> None:
    """Conflicting targets and mislabeled sources fail before an alias mutation."""
    # Test a differing target digest and a source whose expected version does not match.
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "out"
        conflict = FakeRunner(inspection_replies() + inspection_replies(digest=CHILD))
        assert registry_module.main(alias_args(output), registry_module.Registry(conflict)) == 2
        wrong_labels = dict(LABELS); wrong_labels["org.opencontainers.image.version"] = "wrong"
        mismatch = FakeRunner(inspection_replies(metadata=image_metadata(wrong_labels)))
        assert registry_module.main(alias_args(output), registry_module.Registry(mismatch)) == 2


def test_move_replaces_a_differing_target_and_postverifies() -> None:
    """A policy-approved move updates a mutable target with a different digest."""
    # Queue source inspection, differing target, update, and final target inspection.
    fake = FakeRunner(inspection_replies() + inspection_replies(digest=CHILD) + [result()] + inspection_replies())
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "out"
        assert registry_module.main(move_args(output), registry_module.Registry(fake)) == 0
        assert output.read_text().endswith(f"digest={DIGEST}\naction=moved\n")
    assert any(call[3] == "create" for call in fake.calls)


def test_move_matching_target_is_a_noop_without_create() -> None:
    """An already matching mutable target is verified without a registry update."""
    # Queue source, target, and post-check inspections with no create reply.
    fake = FakeRunner(inspection_replies() + inspection_replies() + inspection_replies())
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "out"
        assert registry_module.main(move_args(output), registry_module.Registry(fake)) == 0
        assert output.read_text().endswith("action=noop\n")
    assert not any(call[3] == "create" for call in fake.calls)


def verify_args(output: Path, attempts: str = "2") -> list[str]:
    """Return a complete verification command for the standard immutable tag."""
    return ["verify", "--image", IMAGE, "--digest", DIGEST, "--tag", "v1", "--expected-version", "1.2.3", "--expected-revision", "c" * 40, "--expected-source", "https://example.test/repo", "--attempts", attempts, "--github-output", str(output)]


def test_verify_success_retry_exhaustion_and_conflict_behavior() -> None:
    """Verify retries only absence and transient reads, not conflicting identities."""
    # Check immediate success, a bounded retry, exhaustion, and a non-retryable conflict.
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "out"
        assert registry_module.main(verify_args(output), registry_module.Registry(FakeRunner(inspection_replies()))) == 0
        waits: list[float] = []
        retry = registry_module.Registry(FakeRunner([result(code=1, stderr="manifest unknown")] + inspection_replies()), waits.append)
        assert registry_module.main(verify_args(output), retry) == 0 and waits == [0.25]
        assert registry_module.main(verify_args(output), registry_module.Registry(FakeRunner([result(code=1, stderr="manifest unknown")] * 2))) == 2
        assert registry_module.main(verify_args(output), registry_module.Registry(FakeRunner(inspection_replies(digest=CHILD)))) == 2


def test_github_output_rejects_symlink_and_fifo_targets() -> None:
    """Workflow output writing rejects non-regular paths before they are opened."""
    # Exercise both a link and a FIFO, which must not be followed or block writes.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"; target.write_text("")
        link = root / "link"; link.symlink_to(target)
        fifo = root / "fifo"; os.mkfifo(fifo)
        for path in (link, fifo):
            try:
                registry_module.append_github_output(path, {"digest": DIGEST})
            except registry_module.RegistryError:
                pass
            else:
                raise AssertionError("accepted an unsafe output path")
