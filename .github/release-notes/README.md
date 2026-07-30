# Release notes

One file per release, named `v<version>.md`, holding the hand-written copy for that release's GitHub release card.

`.github/workflows/release.yml` requires the file matching the tag being released and fails before publishing anything if it is missing, so the copy is written and reviewed before the release goes out rather than corrected afterwards.

## Format

This is Chris's cross-project release-notes template, not a shape specific to cwcli. The full published card is:

```markdown
## [<version>](<diff url>) (<date>)

> Description

### Upgrade Steps
* [ACTION REQUIRED] <each manual step a consumer must take>

### Breaking Changes
* <a change that breaks existing usage>

### New Features
* <a new capability>

### Bug Fixes
* <a fix>

### Performance Improvements
* <a speed or resource improvement>

### Other Changes
* <anything else worth a line, e.g. docs>
```

**What goes in `v<version>.md`:** everything from the `> Description` line down. The header line (`## [<version>](<diff url>) (<date>)`) is mechanical - `.github/scripts/release-body.sh` generates it from the version, the previous release tag, and the day the tag is published, so it cannot drift and is never hand-written.

## Rules

1. **Any section that does not apply to this release is omitted entirely** - heading and all. An empty section (a heading with no bullets under it) is never published. Most releases only need two or three of the six sections.
2. **Upgrade Steps flags every manual step with `[ACTION REQUIRED]`.** This section exists specifically to call out action a consumer must take; `release-body.sh` rejects a bullet in this section that is not flagged.
3. **The six section headings are fixed** - `Upgrade Steps`, `Breaking Changes`, `New Features`, `Bug Fixes`, `Performance Improvements`, `Other Changes` - spelled exactly as shown. `release-body.sh` rejects any other `### ` heading in the file, so a typo fails the release instead of publishing as an unrecognised section.
4. **Write plain, concrete bullets**, not a generated commit list and not marketing copy. State what changed and, where it matters, why a reader would care. One bullet per change is normal; keep each to a sentence or two.
5. **The release object's title is set by the workflow**, not this file: `caffeinated-whale-cli: v<version>`, with the colon, never a bare version number. Do not repeat the name or version anywhere in this file's body.

This repo additionally never mentions the unreleased Console/`serve` command, or any GUI, in a release note - it ships unreachable (see `AGENTS.md`'s "Console" entry), and advertising an unreachable command would be worse than saying nothing.

The `CHANGELOG.md` entry for the same release is a different, separate artifact in Keep a Changelog format - the itemised in-repo record, not the release card. This template governs the release card only; do not restructure `CHANGELOG.md` to match it.

## Rendering it before you tag

```bash
.github/scripts/release-body.sh 2.1.0 karotkriss/caffeinated-whale-cli
```

That prints the exact body the workflow will publish.
