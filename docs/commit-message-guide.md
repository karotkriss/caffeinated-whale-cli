# Commit Message Guide

## Overview

This guide defines the commit message conventions for the caffeinated-whale-cli project based on [Conventional Commits](https://www.conventionalcommits.org/) with project-specific patterns observed in the codebase.

## Format

```
<type>: <subject>

[optional body]

[optional footer]
```

### Examples

```
feat: tab completion

Add intelligent tab completion for project names, apps, and sites.
Completions are context-aware and use 2-second TTL caching for performance.

- Project names: queried from Docker in real-time
- Apps/Sites: loaded from cached project data
- Supports Bash, Zsh, Fish, PowerShell
```

```
fix: ctrl + c loop in start

Prevent infinite loop when user presses Ctrl+C during project start.
Exit gracefully instead of re-prompting.
```

```
chore: bump version

Increment version to 0.9.1 for release.
```

## Commit Types

### Primary Types (Use these)

| Type | Purpose | When to Use | Example |
|------|---------|-------------|---------|
| **feat** | New feature | Adding new functionality | `feat: tab completion` |
| **fix** | Bug fix | Fixing incorrect behavior | `fix: ctrl + c loop in start` |
| **chore** | Maintenance | Version bumps, dependencies, housekeeping | `chore: bump version` |
| **docs** | Documentation | README, docs, comments | `docs: add testing guide` |
| **refactor** | Code restructure | Improving structure without changing behavior | `refactor: utils, port utils, start` |
| **test** | Tests | Adding or updating tests | `test: add tests for tab completion` |
| **perf** | Performance | Performance improvements | `perf: optimize Docker queries` |
| **style** | Code style | Formatting, linting fixes | `style: format with black` |
| **build** | Build system | Build scripts, dependencies | `build: update pyproject.toml` |
| **ci** | CI/CD | GitHub Actions, workflows | `ci: add test workflow` |
| **revert** | Revert commit | Reverting previous changes | `revert: feat: tab completion` |

### Project-Specific Patterns

Based on historical commits, these patterns are also used:

| Pattern | Equivalent To | Example | Notes |
|---------|---------------|---------|-------|
| `update: CHANGELOG` | `chore:` | `chore: update CHANGELOG` | Prefer `chore:` |
| `bump: version` | `chore:` | `chore: bump version` | Prefer `chore:` |
| `add: ci and docs` | `feat:` or `docs:` | `feat: add CI/CD workflows` | Be specific |
| `init: port utils` | `feat:` | `feat: add port utilities` | Use `feat:` for new modules |
| `rework: start and stop` | `refactor:` | `refactor: start and stop commands` | Prefer `refactor:` |

**Recommendation:** Stick to the primary types for consistency.

## Subject Line Rules

### ✅ Do

- Use lowercase after the colon: `feat: add feature` not `feat: Add feature`
- Be concise (50 characters or less)
- Use imperative mood: "add" not "added" or "adds"
- Don't end with a period
- Be specific about what changed

**Good Examples:**
```
feat: tab completion
fix: ctrl + c loop in start
chore: bump version
docs: add testing guide
refactor: port conflict detection
test: add completion utils tests
```

### ❌ Don't

- Don't capitalize after colon: ~~`feat: Add Feature`~~
- Don't use past tense: ~~`feat: added feature`~~
- Don't be vague: ~~`fix: bug fix`~~ or ~~`chore: updates`~~
- Don't end with period: ~~`feat: new feature.`~~
- Don't mix types: ~~`feat/fix: thing`~~

**Bad Examples:**
```
❌ feat: Add Feature              # Capitalized
❌ fix: fixed bug                 # Past tense
❌ chore: updates                 # Vague
❌ feat: new feature.             # Period
❌ Fixed Open Frappe App...       # No type prefix
❌ update: readme                 # Use docs: or chore:
```

## Body (Optional but Recommended)

Add a body for:
- Complex changes
- Breaking changes
- Context that isn't obvious from the subject

### Format

- Wrap at 72 characters
- Explain **what** and **why**, not how
- Use bullet points for lists
- Separate from subject with blank line

### Example

```
feat: port conflict detection

Add intelligent port conflict detection before starting projects.
System distinguishes between Frappe projects and external processes.

Features:
- Interactive prompt to stop conflicting Frappe projects
- Cross-platform process identification
- Mixed conflict scenario handling
- Re-validation after stopping Frappe projects

Resolves issue where users had to manually identify and stop
conflicting containers.
```

## Footer (Optional)

Use for:
- Breaking changes
- Issue references
- Co-authors

### Breaking Changes

```
feat: new CLI argument format

BREAKING CHANGE: --project flag renamed to --name
Users must update scripts using --project flag.
```

### Issue References

```
fix: inspect cache invalidation

Closes #42
Fixes #38, #41
See also #40
```

### Co-Authors

```
feat: tab completion

Co-authored-by: Jane Developer <jane@example.com>
```

## Observed Commit Patterns (Historical Analysis)

### By Type Frequency

From 79 analyzed commits:

| Type | Count | Percentage |
|------|-------|------------|
| `chore:` | 29 | 36.7% |
| `fix:` | 24 | 30.4% |
| `feat:` | 16 | 20.3% |
| Other | 10 | 12.6% |

### Common `chore:` Patterns

```
chore: bump version          (12 occurrences)
chore: update CHANGELOG      (10 occurrences)
chore: update README         (5 occurrences)
```

**Pattern:** Most chores are version/release related.

### Common `fix:` Patterns

```
fix: <specific issue>        (Most fixes are descriptive)
fix: ctrl + c loop in start
fix: mix port conflict scenario over sight
fix: inspect before start if no cache is detected
```

**Pattern:** Fixes are specific and describe the issue being resolved.

### Common `feat:` Patterns

```
feat: <feature name>         (All features named clearly)
feat: tab completion
feat: unlock command
feat: update app command
feat: bench status
```

**Pattern:** Features name the capability being added.

## Release Workflow Commits

For version releases, use this sequence:

```bash
# 1. Update version in code
git commit -m "chore: bump version to 0.9.2"

# 2. Update CHANGELOG
git commit -m "chore: update CHANGELOG for v0.9.2"

# 3. Update README (if needed)
git commit -m "chore: update README with new features"

# 4. Tag release (not a commit, but part of workflow)
git tag v0.9.2
```

**Note:** Consider using a single commit for releases:

```
chore: release v0.9.2

- Bump version to 0.9.2
- Update CHANGELOG
- Update README with new features
```

## Scopes (Optional)

Add scope for clarity in large projects:

```
feat(completion): add tab completion
fix(docker): handle connection errors
chore(deps): update dependencies
docs(api): add endpoint documentation
test(utils): add port utility tests
```

Current project doesn't use scopes, but they're helpful as the project grows.

## Multi-Line Commits

For commits with multiple related changes:

```
feat: tab completion

Add intelligent tab completion for CLI:
- Project name completion from Docker
- App name completion from cache
- Site name completion from cache
- Context-aware suggestions
- 2-second TTL caching

Supports all shells: Bash, Zsh, Fish, PowerShell
Documentation added to README

Co-authored-by: Claude <noreply@anthropic.com>
```

## Merge Commits

For pull requests:

```
Merge pull request #6 from karotkriss/ports

feat: port conflict detection and resolution
```

**Better approach:** Use squash merges with conventional commit format:

```
feat: port conflict detection (#6)

Merged from ports branch.
Adds intelligent port conflict detection and interactive resolution.
```

## Common Mistakes Observed

### 1. Inconsistent Type Usage

```
❌ update: CHANGELOG          # Use chore:
❌ bump: version             # Use chore:
❌ init: port utils          # Use feat:
❌ add: ci and docs          # Use feat: or docs:
```

### 2. Non-Descriptive Messages

```
❌ chore: updates            # What was updated?
❌ fix: bug                  # Which bug?
❌ feat: improvements        # What improvements?
```

### 3. Capitalization Issues

```
❌ feat: Add Feature         # Should be lowercase
❌ Fix: bug fix              # Type should be lowercase
✅ feat: add feature         # Correct
```

### 4. Missing Type Prefix

```
❌ Fixed Open Frappe App with custom bench path
❌ Added Init Command
❌ Enhance cwcli init command with new features

✅ fix: open frappe app with custom bench path
✅ feat: add init command
✅ feat: enhance init command with new features
```

## Tools

### Commitlint

To enforce these rules, use commitlint:

```bash
# Install
npm install --save-dev @commitlint/{cli,config-conventional}

# Configure
echo "module.exports = {extends: ['@commitlint/config-conventional']}" > commitlint.config.js

# Use with husky
npm install --save-dev husky
npx husky install
npx husky add .husky/commit-msg 'npx --no -- commitlint --edit $1'
```

### Git Template

Create a commit template:

```bash
# Create template
cat > ~/.gitmessage << 'EOF'
# <type>: <subject>
#
# [optional body]
#
# [optional footer]
#
# Types: feat, fix, chore, docs, refactor, test, perf, style, build, ci, revert
EOF

# Configure git to use it
git config --global commit.template ~/.gitmessage
```

## Quick Reference

### Commit Type Decision Tree

```
Is it a new feature?           → feat:
Is it fixing a bug?            → fix:
Is it version/release related? → chore:
Is it documentation?           → docs:
Is it restructuring code?      → refactor:
Is it adding tests?            → test:
Is it performance?             → perf:
Is it formatting/linting?      → style:
Is it build/dependencies?      → build:
Is it CI/CD related?           → ci:
```

### Template Examples

**Feature:**
```
feat: <feature name>

<what it does>

<additional context if needed>
```

**Bug Fix:**
```
fix: <issue description>

<what was wrong>
<how it's fixed>
```

**Chore:**
```
chore: <maintenance task>

<details if needed>
```

## Best Practices

1. **Write commits atomically** - One logical change per commit
2. **Write for others** - Commits are documentation
3. **Be consistent** - Follow the established patterns
4. **Be specific** - "fix: port conflict detection" not "fix: ports"
5. **Use imperative mood** - "add" not "added" or "adds"
6. **Keep subject short** - Details go in the body
7. **Explain why, not how** - Code shows how, commit explains why

## Resources

- [Conventional Commits](https://www.conventionalcommits.org/)
- [Angular Commit Guidelines](https://github.com/angular/angular/blob/master/CONTRIBUTING.md#commit)
- [How to Write a Git Commit Message](https://chris.beams.io/posts/git-commit/)
- [Commitlint](https://commitlint.js.org/)
