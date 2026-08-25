#!/usr/bin/env python3
"""Validate container release inputs and make registry tag decisions."""

from __future__ import annotations

import argparse
import ast
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


SOURCE = "https://github.com/ShadyF/meeting-recorder"
MAX_INPUT_LENGTH = 256
MAX_SOURCE_FILE_BYTES = 65_536
_IDENTIFIER_RE = re.compile(r"^[0-9A-Za-z-]+$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True, order=True)
class SemVer:
    """A parsed semantic version without build metadata."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if not self.prerelease else f"{core}-{'.'.join(self.prerelease)}"


@dataclass(frozen=True)
class ImmutableDecision:
    action: str
    digest: str | None


@dataclass(frozen=True)
class LatestDecision:
    action: str


def _bounded_ascii(value: str, name: str) -> None:
    """Reject untrusted values that cannot safely be treated as short tokens."""
    # Limit size and reject control or non-ASCII characters before parsing tokens.
    if not value or len(value) > MAX_INPUT_LENGTH or not value.isascii() or "\n" in value or "\r" in value:
        raise ValueError(f"invalid {name}")


def _parse_number(value: str, name: str) -> int:
    """Parse a SemVer numeric identifier without allowing leading zeroes."""
    # Keep numeric identifiers canonical so lexical aliases cannot compare equally.
    if not value.isdigit() or (len(value) > 1 and value[0] == "0"):
        raise ValueError(f"invalid {name}")
    return int(value)


def parse_semver(value: str) -> SemVer:
    """Parse a strict SemVer value without a leading tag prefix or build data."""
    # Reject broad input forms before splitting the version into its identifiers.
    _bounded_ascii(value, "version")
    if "+" in value:
        raise ValueError("build metadata is not allowed")

    # Split the optional prerelease section while requiring a complete core version.
    core, separator, prerelease_text = value.partition("-")
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError("invalid version")
    version = SemVer(
        _parse_number(parts[0], "version"),
        _parse_number(parts[1], "version"),
        _parse_number(parts[2], "version"),
    )

    # Validate every prerelease identifier before storing the immutable tuple.
    if separator:
        identifiers = tuple(prerelease_text.split("."))
        if not prerelease_text or any(
            not identifier
            or not _IDENTIFIER_RE.fullmatch(identifier)
            or (identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0")
            for identifier in identifiers
        ):
            raise ValueError("invalid prerelease")
        return SemVer(version.major, version.minor, version.patch, identifiers)
    return version


def compare_semver(left: SemVer, right: SemVer) -> int:
    """Compare versions using SemVer precedence, including prerelease rules."""
    # Compare the numeric core before applying prerelease precedence.
    left_core = (left.major, left.minor, left.patch)
    right_core = (right.major, right.minor, right.patch)
    if left_core != right_core:
        return (left_core > right_core) - (left_core < right_core)

    # A normal release has higher precedence than any prerelease of the same core.
    if not left.prerelease or not right.prerelease:
        return (bool(right.prerelease) > bool(left.prerelease)) - (bool(right.prerelease) < bool(left.prerelease))

    # Compare prerelease identifiers one at a time, then use identifier count.
    for left_id, right_id in zip(left.prerelease, right.prerelease):
        if left_id == right_id:
            continue
        left_numeric = left_id.isdigit()
        right_numeric = right_id.isdigit()
        if left_numeric and right_numeric:
            return (int(left_id) > int(right_id)) - (int(left_id) < int(right_id))
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return (left_id > right_id) - (left_id < right_id)
    return (len(left.prerelease) > len(right.prerelease)) - (len(left.prerelease) < len(right.prerelease))


def parse_release_tag(tag: str) -> SemVer:
    """Parse a release tag in the required vX.Y.Z[-prerelease] form."""
    # Require the tag prefix here so bare project versions use parse_semver instead.
    _bounded_ascii(tag, "tag")
    if not tag.startswith("v"):
        raise ValueError("invalid release tag")
    return parse_semver(tag[1:])


def normalize_revision(revision: str) -> str:
    """Validate a full Git revision and return its lowercase representation."""
    # Accept only a full object ID so short revisions cannot create ambiguous tags.
    _bounded_ascii(revision, "revision")
    if not _REVISION_RE.fullmatch(revision):
        raise ValueError("invalid revision")
    return revision.lower()


def validate_digest(digest: str | None) -> str | None:
    """Validate an optional registry digest."""
    # Preserve absence while requiring canonical lowercase SHA-256 digests when present.
    if digest is None or digest == "":
        return None
    _bounded_ascii(digest, "digest")
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError("invalid digest")
    return digest


def immutable_policy(version_digest: str | None, sha_digest: str | None) -> ImmutableDecision:
    """Choose the action needed to keep immutable version and SHA tags aligned."""
    # Normalize optional inputs before deciding whether either immutable alias is missing.
    version_digest = validate_digest(version_digest)
    sha_digest = validate_digest(sha_digest)
    if version_digest is None and sha_digest is None:
        return ImmutableDecision("publish", None)
    if version_digest is None:
        return ImmutableDecision("repair-version", sha_digest)
    if sha_digest is None:
        return ImmutableDecision("repair-sha", version_digest)
    if version_digest == sha_digest:
        return ImmutableDecision("noop", version_digest)
    return ImmutableDecision("conflict", None)


def latest_policy(candidate: SemVer, candidate_digest: str, latest: SemVer | None, latest_digest: str | None) -> LatestDecision:
    """Choose whether a stable candidate may update the mutable latest tag."""
    # Require a canonical candidate digest before any branch can skip or move latest.
    if validate_digest(candidate_digest) is None:
        raise ValueError("candidate digest is required")

    # Require the mutable latest version and digest to appear together or not at all.
    latest_digest = validate_digest(latest_digest)
    if (latest is None) != (latest_digest is None):
        raise ValueError("latest version and digest must be provided together")

    # Prerelease images never change the stable latest alias after input validation.
    if candidate.is_prerelease:
        return LatestDecision("skip")
    if latest is None:
        return LatestDecision("move")

    # Move latest only forward, except a matching digest already needs no mutation.
    comparison = compare_semver(candidate, latest)
    if comparison > 0:
        return LatestDecision("move")
    if comparison < 0:
        return LatestDecision("skip")
    if candidate_digest == latest_digest:
        return LatestDecision("noop")
    return LatestDecision("conflict")


def _read_bounded(path: Path) -> str:
    """Read a small text source file without following an unbounded input."""
    # Refuse oversized source files before decoding their static version declarations.
    if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
        raise ValueError("version source is too large")
    return path.read_text(encoding="utf-8")


def read_application_version(project_root: Path) -> str:
    """Read __version__ from a static assignment without importing the application."""
    # Parse Python syntax and accept exactly the simple literal assignment we release.
    source = _read_bounded(project_root / "meeting_recorder" / "__init__.py")
    tree = ast.parse(source, filename="meeting_recorder/__init__.py")
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "__version__"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if len(values) != 1:
        raise ValueError("invalid application version declaration")
    return values[0]


def read_project_version(project_root: Path) -> str:
    """Read project.version from a bounded static TOML section."""
    # Scan only the project section because a full TOML parser is not needed for this value.
    source = _read_bounded(project_root / "pyproject.toml")
    in_project = False
    values: list[str] = []
    for line in source.splitlines():
        section = re.fullmatch(r"\s*\[([^]]+)]\s*", line)
        if section:
            in_project = section.group(1) == "project"
            continue
        match = re.fullmatch(r'\s*version\s*=\s*"([^"]*)"\s*(?:#.*)?', line)
        if in_project and match:
            values.append(match.group(1))
    if len(values) != 1:
        raise ValueError("invalid project version declaration")
    return values[0]


def validate_source_versions(project_root: Path, version: SemVer) -> None:
    """Require both static project version declarations to equal the release version."""
    # Compare string forms so declarations cannot use a non-canonical equivalent spelling.
    expected = str(version)
    if read_application_version(project_root) != expected or read_project_version(project_root) != expected:
        raise ValueError("source versions do not match release tag")


def append_github_output(path: Path, outputs: dict[str, str]) -> None:
    """Append trusted single-line outputs to a regular GitHub output file."""
    # Validate fixed output values before opening the file to prevent output injection.
    if any(not key.isidentifier() or "\n" in value or "\r" in value for key, value in outputs.items()):
        raise ValueError("unsafe output")
    # Reject existing unsafe targets before opening, including FIFOs that could block.
    try:
        existing_mode = path.lstat().st_mode
    except FileNotFoundError:
        existing_mode = None
    if existing_mode is not None and not stat.S_ISREG(existing_mode):
        raise ValueError("github output path is not a regular file")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    # Open without following symlinks and verify the resulting descriptor is regular.
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("github output path is not a regular file")
        payload = "".join(f"{key}={value}\n" for key, value in outputs.items()).encode("ascii")
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)


def _validate_command(arguments: argparse.Namespace) -> int:
    """Validate release inputs and write the workflow values for an image build."""
    # Parse trusted release identifiers and compare both static source declarations.
    version = parse_release_tag(arguments.tag)
    revision = normalize_revision(arguments.revision)
    validate_source_versions(Path(arguments.project_root), version)

    # Emit only normalized values that have passed all release policy checks.
    append_github_output(Path(arguments.github_output), {
        "version": str(version), "version_tag": str(version), "sha_tag": f"sha-{revision}",
        "revision": revision,
        "prerelease": str(version.is_prerelease).lower(), "source": SOURCE,
    })
    return 0


def _immutable_command(arguments: argparse.Namespace) -> int:
    """Write an immutable tag decision for workflow branching."""
    # Evaluate aliases with the same pure policy used by direct callers and tests.
    decision = immutable_policy(arguments.version_digest, arguments.sha_digest)
    outputs = {"action": decision.action}
    if decision.digest is not None:
        outputs["digest"] = decision.digest
    append_github_output(Path(arguments.github_output), outputs)
    return 0


def _latest_command(arguments: argparse.Namespace) -> int:
    """Write a latest-tag decision for workflow branching."""
    # Parse supplied versions before selecting the safe mutable-tag action.
    candidate = parse_semver(arguments.candidate_version)
    latest = parse_semver(arguments.latest_version) if arguments.latest_version is not None else None
    decision = latest_policy(candidate, arguments.candidate_digest, latest, arguments.latest_digest)
    append_github_output(Path(arguments.github_output), {"action": decision.action})
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser used by release workflows."""
    # Keep every subcommand explicit so workflows do not need to reimplement policy.
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--tag", required=True)
    validate.add_argument("--revision", required=True)
    validate.add_argument("--project-root", required=True)
    validate.add_argument("--github-output", required=True)
    validate.set_defaults(handler=_validate_command)
    immutable = commands.add_parser("immutable")
    immutable.add_argument("--version-digest")
    immutable.add_argument("--sha-digest")
    immutable.add_argument("--github-output", required=True)
    immutable.set_defaults(handler=_immutable_command)
    latest = commands.add_parser("latest")
    latest.add_argument("--candidate-version", required=True)
    latest.add_argument("--candidate-digest", required=True)
    latest.add_argument("--latest-version")
    latest.add_argument("--latest-digest")
    latest.add_argument("--github-output", required=True)
    latest.set_defaults(handler=_latest_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the release policy command line interface."""
    # Convert validation failures to concise diagnostics that do not reflect raw input.
    try:
        arguments = build_parser().parse_args(argv)
        return arguments.handler(arguments)
    except (OSError, SyntaxError, ValueError):
        print("container release validation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
