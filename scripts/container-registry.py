#!/usr/bin/env python3
"""Read and safely create immutable container registry aliases with Buildx."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


MAX_INPUT = 256
MAX_JSON = 1_048_576
MAX_OUTPUT = 1_048_576
COMMAND_TIMEOUT = 20.0
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254}$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_MANIFESTS = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_INDEXES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


@dataclass(frozen=True)
class RunResult:
    """The small subprocess result surface used by this module and its tests."""

    returncode: int
    stdout: str
    stderr: str = ""


Runner = Callable[[Sequence[str], float], RunResult]
Sleeper = Callable[[float], None]


class RegistryError(ValueError):
    """A registry response that must not be trusted."""


class NotFound(RegistryError):
    """A registry reference was conclusively absent."""


class Transient(RegistryError):
    """A registry read may succeed after propagation or a temporary failure."""


@dataclass(frozen=True)
class Inspection:
    """The immutable identity and release labels read from a registry object."""

    digest: str
    version: str
    revision: str
    source: str


def _run(arguments: Sequence[str], timeout: float) -> RunResult:
    """Run Buildx without a shell and retain only bounded text responses."""
    # Use an argument vector so input can never be interpreted by a shell.
    completed = subprocess.run(
        list(arguments), shell=False, check=False, capture_output=True, text=True,
        timeout=timeout,
    )
    if len(completed.stdout) > MAX_OUTPUT or len(completed.stderr) > MAX_OUTPUT:
        raise RegistryError("registry response is too large")
    return RunResult(completed.returncode, completed.stdout, completed.stderr)


def _token(value: str, name: str, pattern: re.Pattern[str] | None = None) -> str:
    """Validate a short, printable command-line token."""
    # Reject controls and long values before they reach Docker or output files.
    if not value or len(value) > MAX_INPUT or not value.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise RegistryError(f"invalid {name}")
    if pattern is not None and not pattern.fullmatch(value):
        raise RegistryError(f"invalid {name}")
    return value


def _digest(value: str) -> str:
    """Require the canonical top-level SHA-256 digest representation."""
    # Canonical digests prevent an image ID or child descriptor from being accepted.
    if not _DIGEST.fullmatch(value):
        raise RegistryError("invalid digest")
    return value


def append_github_output(path: Path, outputs: dict[str, str]) -> None:
    """Append trusted single-line values without following unsafe output paths."""
    # Validate data before opening the target so outputs cannot inject workflow syntax.
    if any(not key.isidentifier() or not value.isascii() or "\n" in value or "\r" in value for key, value in outputs.items()):
        raise RegistryError("unsafe output")

    # Reject symlinks, FIFOs, and devices before opening the path.
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and not stat.S_ISREG(mode):
        raise RegistryError("github output path is not a regular file")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    # Verify the opened descriptor too, closing a race between lstat and open.
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RegistryError("github output path is not a regular file")
        payload = "".join(f"{key}={value}\n" for key, value in outputs.items()).encode("ascii")
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)


def _json(text: str) -> object:
    """Decode a bounded JSON response with no trailing non-whitespace data."""
    # Cap registry data before decoding it into Python objects.
    if not text or len(text.encode("utf-8")) > MAX_JSON:
        raise RegistryError("invalid registry json")
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise RegistryError("invalid registry json") from error


def _failure(result: RunResult) -> None:
    """Classify command failures without reflecting registry stderr to users."""
    # Only well-known absence replies are safe to interpret as a missing reference.
    detail = result.stderr.lower()
    if any(marker in detail for marker in ("manifest unknown", "name unknown", "not found", "404")):
        raise NotFound("reference absent")
    if any(marker in detail for marker in ("timeout", "timed out", "network", "connection", "temporary", "429", "500", "502", "503", "504")):
        raise Transient("registry unavailable")
    raise RegistryError("registry command failed")


class Registry:
    """A bounded Buildx adapter with injectable process and wait functions."""

    def __init__(self, runner: Runner = _run, sleeper: Sleeper = time.sleep) -> None:
        self.runner = runner
        self.sleeper = sleeper

    def _command(self, arguments: Sequence[str]) -> str:
        """Run one bounded Buildx command and return its bounded standard output."""
        # Translate OS timeouts into a retryable error without exposing command details.
        try:
            result = self.runner(arguments, COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise Transient("registry timed out") from error
        if result.returncode:
            _failure(result)
        if len(result.stdout) > MAX_OUTPUT:
            raise RegistryError("registry response is too large")
        return result.stdout

    def inspect(self, image: str, reference: str) -> Inspection:
        """Read a tag or digest and prove its top-level identity and labels."""
        # Fetch raw metadata first so index children can be selected safely.
        image = _token(image, "image", _IMAGE)
        reference = _token(reference, "reference", _TAG if "@" not in reference else None)
        full_reference = f"{image}{reference}" if reference.startswith("@") else f"{image}:{reference}"
        raw = _json(self._command(["docker", "buildx", "imagetools", "inspect", "--raw", full_reference]))
        if not isinstance(raw, dict) or not isinstance(raw.get("mediaType"), str):
            raise RegistryError("invalid manifest")

        # Select one real amd64 child and reject every non-attestation sibling.
        media_type = raw["mediaType"]
        child_reference = full_reference
        if media_type in _INDEXES:
            descriptors = raw.get("manifests")
            if not isinstance(descriptors, list) or len(descriptors) > 128:
                raise RegistryError("invalid manifest list")
            # Collect attestations first because valid indexes may place them before the image.
            candidates: list[str] = []
            attestation_subjects: list[str] = []
            for descriptor in descriptors:
                if not isinstance(descriptor, dict):
                    raise RegistryError("invalid manifest descriptor")
                platform = descriptor.get("platform")
                annotations = descriptor.get("annotations", {})
                if not isinstance(platform, dict) or not isinstance(annotations, dict):
                    raise RegistryError("invalid manifest descriptor")
                descriptor_digest = descriptor.get("digest")
                if descriptor.get("mediaType") not in _MANIFESTS or not isinstance(descriptor_digest, str):
                    raise RegistryError("invalid manifest descriptor")
                child_digest = _digest(descriptor_digest)
                if platform.get("os") == "unknown" and platform.get("architecture") == "unknown":
                    if annotations.get("vnd.docker.reference.type") != "attestation-manifest":
                        raise RegistryError("invalid attestation child")
                    subject_digest = annotations.get("vnd.docker.reference.digest")
                    if not isinstance(subject_digest, str):
                        raise RegistryError("invalid attestation child")
                    attestation_subjects.append(_digest(subject_digest))
                    continue
                if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
                    candidates.append(child_digest)
                    continue
                raise RegistryError("unexpected runnable image child")
            # Bind every attestation to the one selected child after all descriptors are known.
            if len(candidates) != 1:
                raise RegistryError("expected one linux amd64 image")
            if not attestation_subjects or any(subject != candidates[0] for subject in attestation_subjects):
                raise RegistryError("invalid attestation binding")
            child_reference = f"{image}@{candidates[0]}"
        elif media_type not in _MANIFESTS:
            raise RegistryError("unexpected manifest media type")

        # Read the root digest separately; Buildx raw data does not carry its own digest.
        root_digest = _digest(self._command(["docker", "buildx", "imagetools", "inspect", "--format", "{{.Manifest.Digest}}", full_reference]).strip())

        # Read the selected image object to prove its platform and obtain config labels.
        image_metadata = _json(
            self._command(["docker", "buildx", "imagetools", "inspect", child_reference, "--format", "{{json .Image}}"])
        )
        if not isinstance(image_metadata, dict):
            raise RegistryError("invalid image metadata")
        if image_metadata.get("os") != "linux" or image_metadata.get("architecture") != "amd64":
            raise RegistryError("unexpected image platform")
        config = image_metadata.get("config")
        if not isinstance(config, dict):
            raise RegistryError("invalid image config")
        labels = config.get("Labels", {})
        if not isinstance(labels, dict):
            raise RegistryError("invalid image labels")
        values: list[str] = []
        for key in ("org.opencontainers.image.version", "org.opencontainers.image.revision", "org.opencontainers.image.source"):
            value = labels.get(key, "")
            if not isinstance(value, str) or len(value) > MAX_INPUT or not value.isascii() or "\n" in value or "\r" in value:
                raise RegistryError("invalid image labels")
            values.append(value)
        return Inspection(root_digest, *values)

    def create(self, image: str, digest: str, tag: str) -> None:
        """Create an alias using Buildx's registry-side copy operation."""
        # Registries have no portable atomic create-only primitive. Workflow concurrency and
        # a restricted writer are the controls; callers recheck before and after this call.
        self._command(["docker", "buildx", "imagetools", "create", "--tag", f"{image}:{tag}", f"{image}@{digest}"])


def _expected(arguments: argparse.Namespace) -> tuple[str, str, str]:
    """Validate the three expected labels before comparing untrusted registry data."""
    return tuple(_token(getattr(arguments, name), name.replace("expected_", "")) for name in ("expected_version", "expected_revision", "expected_source"))  # type: ignore[return-value]


def _require(inspection: Inspection, digest: str, expected: tuple[str, str, str]) -> None:
    """Require an inspection to exactly match the immutable release identity."""
    # Exact equality prevents aliases from silently pointing at a different release.
    if inspection.digest != digest or (inspection.version, inspection.revision, inspection.source) != expected:
        raise RegistryError("registry identity mismatch")


def inspect_command(arguments: argparse.Namespace, registry: Registry) -> int:
    """Inspect one tag and emit its existence, identity, and labels."""
    # Absence is a normal query result; all other failures are command failures.
    try:
        result = registry.inspect(arguments.image, arguments.tag)
    except NotFound:
        append_github_output(Path(arguments.github_output), {"present": "false"})
        return 0
    append_github_output(Path(arguments.github_output), {"present": "true", "digest": result.digest, "version": result.version, "revision": result.revision, "source": result.source})
    return 0


def alias_command(arguments: argparse.Namespace, registry: Registry) -> int:
    """Create a missing immutable alias after proving source and target identity."""
    # Verify the immutable source before any tag mutation can occur.
    digest = _digest(arguments.source_digest)
    expected = _expected(arguments)
    _require(registry.inspect(arguments.image, "@" + digest), digest, expected)

    # Recheck immediately before mutation and never overwrite a conflicting target.
    try:
        target = registry.inspect(arguments.image, arguments.target_tag)
    except NotFound:
        registry.create(_token(arguments.image, "image", _IMAGE), digest, _token(arguments.target_tag, "target tag", _TAG))
        action = "created"
    else:
        if target.digest != digest:
            raise RegistryError("target tag conflict")
        _require(target, digest, expected)
        action = "noop"

    # Re-read the alias so a successful command is never treated as sufficient proof.
    _require(registry.inspect(arguments.image, arguments.target_tag), digest, expected)
    append_github_output(Path(arguments.github_output), {"digest": digest, "action": action})
    return 0


def verify_command(arguments: argparse.Namespace, registry: Registry) -> int:
    """Wait a bounded time for every immutable tag to become consistently visible."""
    # Validate inputs once before bounded propagation reads begin.
    digest = _digest(arguments.digest)
    expected = _expected(arguments)
    if not 1 <= arguments.attempts <= 10:
        raise RegistryError("invalid attempts")
    tags = [_token(tag, "tag", _TAG) for tag in arguments.tag]

    # Retry only absent or transient reads; malformed and conflicting data fails at once.
    for attempt in range(arguments.attempts):
        try:
            for tag in tags:
                _require(registry.inspect(arguments.image, tag), digest, expected)
            append_github_output(Path(arguments.github_output), {"digest": digest})
            return 0
        except (NotFound, Transient):
            if attempt == arguments.attempts - 1:
                raise RegistryError("registry verification did not converge")
            registry.sleeper(min(8.0, 0.25 * (2 ** attempt)))
    raise AssertionError("unreachable")


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit command line interface for registry workflows."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--image", required=True); inspect.add_argument("--tag", required=True); inspect.add_argument("--github-output", required=True)
    alias = commands.add_parser("alias")
    alias.add_argument("--image", required=True); alias.add_argument("--source-digest", required=True); alias.add_argument("--target-tag", required=True)
    alias.add_argument("--expected-version", required=True); alias.add_argument("--expected-revision", required=True); alias.add_argument("--expected-source", required=True); alias.add_argument("--github-output", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--image", required=True); verify.add_argument("--digest", required=True); verify.add_argument("--tag", action="append", required=True)
    verify.add_argument("--expected-version", required=True); verify.add_argument("--expected-revision", required=True); verify.add_argument("--expected-source", required=True); verify.add_argument("--attempts", type=int, required=True); verify.add_argument("--github-output", required=True)
    return parser


def main(argv: Sequence[str] | None = None, registry: Registry | None = None) -> int:
    """Run the adapter and present a concise, redacted error to workflows."""
    # Do not expose registry URLs, credentials, or untrusted stderr in workflow logs.
    try:
        arguments = build_parser().parse_args(argv)
        active_registry = registry or Registry()
        return {"inspect": inspect_command, "alias": alias_command, "verify": verify_command}[arguments.command](arguments, active_registry)
    except (OSError, RegistryError, subprocess.SubprocessError):
        print("container registry operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
