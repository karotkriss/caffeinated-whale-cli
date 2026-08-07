# SignPath signing runbook (maintainer)

This is the maintainer checklist for enabling signed Windows releases through SignPath Foundation.
The engineering is already merged and inert: the `windows-exe` job in [`.github/workflows/release.yml`](../../.github/workflows/release.yml) builds an unsigned `cwcli.exe` on every release today, and switches to signing the moment the two credentials in step 4 exist.
The written policy SignPath asks for is [code-signing.md](./code-signing.md); this document is the "what do I actually click" companion.

Nothing here can be done by an automated agent.
The application is an outward-facing act of consent that only the project owner can make, and the per-release approval is the deliberate human control the program is built around.

## One-time setup

### 1. Apply to SignPath Foundation

Go to <https://signpath.org/> and choose "Apply for Free Code Signing" (the open-source program).
The application asks for:

- **Repository URL:** `https://github.com/karotkriss/caffeinated-whale-cli`
- **Download URL:** `https://github.com/karotkriss/caffeinated-whale-cli/releases`
- **License:** MIT
- **Project description:** a short line, e.g. "A command-line tool to create, manage, and back up local Frappe/ERPNext Docker development instances."

Accept the Foundation terms, including the "no security-circumvention" clause and the requirement that every release be manually approved.
Approval is a human review by the Foundation, not instant; expect a wait.

### 2. Turn on MFA everywhere

The program requires multi-factor authentication on both accounts:

- **GitHub:** the `karotkriss` account. (Settings -> Password and authentication -> Two-factor authentication.)
- **SignPath:** enable MFA on the SignPath account during or right after onboarding.

### 3. Complete SignPath onboarding

During onboarding, SignPath walks you through creating:

- an **organization** (note its **organization id**, a GUID);
- a **project** for cwcli. Use the slug **`caffeinated-whale-cli`** so it matches the workflow, or change `project-slug` in the `windows-exe` job to whatever slug you pick;
- a **signing policy**. Use the slug **`release-signing`** so it matches the workflow, or change `signing-policy-slug` to match;
- the **SignPath GitHub App**, installed on the `caffeinated-whale-cli` repository (owner-level GitHub permission);
- with the **Trusted Build System** set to **GitHub.com**;
- a **submitter API token** for the signing request.

If you pick different slugs, the only edit is those two `with:` lines in the `windows-exe` job; nothing else changes.

### 4. Add the two credentials to the repository

This is the switch that turns signing on.
In the `caffeinated-whale-cli` repository, under Settings -> Secrets and variables -> Actions:

| Kind | Name | Value |
| --- | --- | --- |
| **Secret** | `SIGNPATH_API_TOKEN` | the submitter API token from onboarding |
| **Variable** | `SIGNPATH_ORGANIZATION_ID` | the organization id (GUID) from onboarding |

The name and kind must match exactly.
`SIGNPATH_API_TOKEN` is a secret; `SIGNPATH_ORGANIZATION_ID` is a plain variable (it is not sensitive, and the signing action reads it as one).
Until both exist, the release ships the unsigned `cwcli.exe` and the SignPath steps are skipped cleanly.

## Per-release approval

Once set up, every release needs one manual approval:

1. Cut the release as usual (bump the version, write the release card, push the `vX.Y.Z` tag). See the [CI/CD release guide](./ci-cd.md#release-githubworkflowsreleaseyml).
2. The release workflow builds the unsigned exe and submits it to SignPath. The `windows-exe` job then **waits** on your approval; a paused job here is expected, not a hang.
3. Open the pending signing request in the SignPath web UI, confirm it is the release you just tagged, and click approve.
4. SignPath signs the binary and returns it, and the workflow attaches the signed `cwcli.exe` to the GitHub release.

If you never approve, the request eventually times out and the release keeps the unsigned binary.
The PyPI publish and the Homebrew tap bump are unaffected either way; they never depend on this job.

## What signing does and does not fix

Set expectations honestly (the full analysis is in the feasibility scout):

- **Fixed immediately:** the "unknown publisher" label (it becomes "SignPath Foundation") and antivirus false-positive churn.
- **Cleared only over time:** the SmartScreen "Windows protected your PC" warning and Smart App Control both build trust from download volume, which a niche CLI accrues slowly.
- **Not fixed by us:** an organization running WDAC in enforced allow-list mode must still add our publisher or the file hash to their policy. Signing turns "impossible" into a concrete IT request, but the request is still theirs to grant.

## Related

- [code-signing.md](./code-signing.md) - the written policy SignPath requires.
- [ci-cd.md](./ci-cd.md) - the full release workflow inventory.
