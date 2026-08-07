# Code-signing policy

This is the written code-signing policy for the Caffeinated Whale CLI Windows binary.
SignPath Foundation requires such a policy as a condition of its free open-source signing program, and this document is its canonical statement.
It describes what is signed, how the build is produced, who is accountable, and how each signing request is approved.

The operational side (how to apply, which secrets to add, how to approve a release) lives in [signpath-runbook.md](./signpath-runbook.md).
The workflow that implements this policy is the `windows-exe` job in [`.github/workflows/release.yml`](../../.github/workflows/release.yml); the inventory of all release jobs is in [ci-cd.md](./ci-cd.md).

## What is signed

Exactly one artifact: the standalone Windows executable `cwcli.exe`.
It is produced by compiling the published `caffeinated-whale-cli` source with Nuitka (`--onefile`) into a single native binary that needs no Python on the end user's machine.
Nothing else is signed: the PyPI wheel and sdist, the Homebrew formula, and the source tree are all distributed unsigned exactly as before.

The signature is an OV (organization-validation) Authenticode certificate issued in the name of "SignPath Foundation".
The Windows publisher string therefore reads "SignPath Foundation", not "Caffeinated Whale" or the maintainer's name.
This is a deliberate, accepted trade-off: it is the identity the free program signs under, and it is what makes the program independent of the maintainer's jurisdiction.

## Build provenance

Every signed build is reproducible in origin, if not byte-for-byte, and this provenance is what SignPath verifies before it will sign.

- **Tag-triggered.** The release workflow runs only on a pushed `v*.*.*` tag. There is no manual `workflow_dispatch` path to the signing job and no way to sign an arbitrary commit.
- **GitHub-hosted, end to end.** Every job that feeds the signing request runs on GitHub-hosted runners (`windows-latest` for the build, `ubuntu-latest` for the jobs before it). No self-hosted runner touches the artifact. SignPath's OSS program requires this and uses GitHub-provided run metadata to prove the build was not forged.
- **From the public repository.** The build checks out `karotkriss/caffeinated-whale-cli` at the tagged commit and compiles the same source that is published to PyPI. The `__version__` compiled into the binary is the repository's version literal for that tag.
- **No secret build inputs.** The build step needs no credentials. Only the signing step consumes the SignPath API token, and only to submit the already-built artifact.

## Roles

SignPath Foundation defines three roles for a signing request: the Author (who wrote and committed the code), the Reviewer (who reviewed it), and the Approver (who authorizes the signature).

Caffeinated Whale CLI is maintained by a single maintainer.
All three roles collapse onto that maintainer.
This is explicitly permitted for solo-maintained projects, and it does not weaken the control: the per-release manual approval below is still a distinct, deliberate human act, separate from merging or tagging.

## Per-release approval

Signing is not automatic even though the workflow is.
For each release:

1. The maintainer pushes the version tag; the release workflow builds the unsigned `cwcli.exe` and submits it to SignPath.
2. The workflow's signing step blocks, waiting for approval. This wait is by design, not a failure.
3. The maintainer opens the pending signing request in the SignPath web UI, confirms it corresponds to the intended release, and manually approves it.
4. SignPath signs the binary and returns it; the workflow attaches the signed `cwcli.exe` to the GitHub release.

No release is ever signed without that explicit approval click.
If the approval is never given, the request times out and the release simply carries the unsigned binary; nothing else about the release is affected.

## Safe-to-merge / disabled state

The signing path is inert until the SignPath credentials are configured.
The `windows-exe` job gates the entire signing sequence on the presence of the `SIGNPATH_API_TOKEN` secret and the `SIGNPATH_ORGANIZATION_ID` variable.
When they are absent, the job builds the unsigned `cwcli.exe` and attaches it to the release, and skips SignPath entirely.
A release therefore works end to end whether or not signing is enabled, and this policy takes effect the moment the credentials are added.
