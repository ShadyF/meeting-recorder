# Container Images

## Status and scope

Issue #27 defines the intended GHCR release behavior for the runtime image. It
does not mean that a public image is available yet. The target image name is
`ghcr.io/shadyf/meeting-recorder`. The first package publish defaults to private:
the package owner must make it public manually, then rerun the complete release
workflow. Issue #27 remains open until an anonymous digest pull and hardened
digest smoke both succeed.

This document covers image publishing and consumption. It is not a deployment,
installation, auto-update, or graphical-capture guide. Debian/APT releases stay
separate and unchanged. Quadlet installation, update, rollback, confinement,
and lifecycle are #28; graphical Bluefin validation is #29.

## Release input and output

The GHCR workflow is separate from other release workflows and uses only the
permissions needed to publish and attest the image. It runs when a maintainer
pushes a protected, manually managed Git tag in the form `vX.Y.Z` or
`vX.Y.Z-prerelease`. Semantic-release is deliberately deferred to a later
release-pipeline issue.

For a release commit, the workflow publishes these tags:

| Tag | Applies to | Meaning |
|---|---|---|
| `X.Y.Z[-prerelease]` | Every accepted release tag | The bare version from the Git tag. |
| `sha-<full commit SHA>` | Every accepted release tag | The exact release commit. |
| `latest` | Stable releases only | A convenience reference to the newest accepted stable release. |

Version and SHA tags are immutable. Re-running a release that already points a
tag at the same digest is a no-op; trying to reuse either tag for a different
digest fails. `latest` moves only forward to a newer stable version and never
rolls back. Prereleases do not receive or move `latest`.

## Provenance and build limits

The publishing workflow pins GitHub Actions by full commit SHA and publishes
BuildKit maximum provenance plus an SBOM for the released digest. Those
attestations support source and provenance reproducibility; they do not make the
result a byte-for-byte reproducible rebuild. The runtime `Containerfile` pins
its Ubuntu base image, but its live APT inputs can change between builds.

The workflow's anonymous hardened digest smoke is release verification only. It
does not install the image, configure a host, test graphical capture, or update
an existing installation. No repository workflow or sample configuration pulls
or updates a user's image automatically.

## Operator consumption

When a published digest has passed the release checks, consume it by digest, not
by a mutable tag. For example:

```text
ghcr.io/shadyf/meeting-recorder@sha256:<released-digest>
```

The version and full-SHA tags help discovery and traceability. `latest` is a
convenience tag only and is never a deployment pin. Operators should record the
chosen digest and use #28's manual install/update/rollback guidance when it is
available.

Image publication and its attestations do not replace the runtime host contract:
the desktop services, sockets, writable paths, secret handling, and deployment
restrictions remain outside the image. See the [runtime image section in the
README](../README.md#runtime-image) for the image/runtime boundary.
