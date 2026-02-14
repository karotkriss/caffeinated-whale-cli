# Chores Guide

## Overview

"Chores" are maintenance tasks that keep the project healthy but don't add features or fix bugs. This guide defines what chores are, when to do them, and how to manage them effectively.

Based on analysis of 79 commits in caffeinated-whale-cli, **chores represent 36.7% of all commits**, making them the most frequent commit type in the project.

## What is a Chore?

**Definition:** Maintenance work that doesn't modify production code behavior.

### Chores Include:

✅ Version bumps
✅ Dependency updates
✅ CHANGELOG updates
✅ README updates (non-feature)
✅ Build configuration
✅ Tooling configuration
✅ Code cleanup
✅ Repository maintenance
✅ License updates
✅ .gitignore updates

### Not Chores:

❌ Bug fixes → `fix:`
❌ New features → `feat:`
❌ Code refactoring → `refactor:`
❌ Documentation additions → `docs:`
❌ Test additions → `test:`

## Common Chore Types

### Analysis from Project History

From 29 chore commits analyzed:

| Chore Type | Count | % of Chores |
|-----------|-------|-------------|
| Version bumps | 12 | 41.4% |
| CHANGELOG updates | 10 | 34.5% |
| README updates | 5 | 17.2% |
| Other | 2 | 6.9% |

**Insight:** Most chores are release-related (version, CHANGELOG, README).

## Chore Patterns

### 1. Version Bumps

**When:** Before every release

**Pattern:**
```
chore: bump version to 0.9.2
```

**Files Changed:**
- `pyproject.toml` - Update `version = "0.9.2"`
- `src/caffeinated_whale_cli/__init__.py` - Update `__version__ = "0.9.2"`

**Process:**
```bash
# 1. Update version in pyproject.toml
# version = "0.9.2"

# 2. Update version in __init__.py
# __version__ = "0.9.2"

# 3. Commit
git add pyproject.toml src/caffeinated_whale_cli/__init__.py
git commit -m "chore: bump version to 0.9.2"
```

**Avoid These Patterns:**
```
❌ bump: version                # Use chore: prefix
❌ chore: bumb version          # Typo: "bumb"
❌ chore: bump version          # Include version number
❌ update: version              # Use chore: prefix
```

**Best Practice:**
```
✅ chore: bump version to 0.9.2
```

### 2. CHANGELOG Updates

**When:** Before every release, document all changes

**Pattern:**
```
chore: update CHANGELOG for v0.9.2
```

**Files Changed:**
- `CHANGELOG.md` - Add release notes

**Process:**
```bash
# 1. Update CHANGELOG.md with new version section
# 2. Document all changes since last release
git add CHANGELOG.md
git commit -m "chore: update CHANGELOG for v0.9.2"
```

**CHANGELOG Format:**
```markdown
# Changelog

## [0.9.2] - 2025-11-08

### Added
- Tab completion for projects, apps, and sites
- Test coverage for completion utilities

### Fixed
- Ctrl+C loop in start command
- Port conflict detection for mixed scenarios

### Changed
- Improved error messages for Docker connection issues
```

**Avoid:**
```
❌ update: CHANGELOG            # Use chore: prefix
❌ chore: update changelog      # Capitalize CHANGELOG
```

**Best Practice:**
```
✅ chore: update CHANGELOG for v0.9.2
```

### 3. README Updates

**When:** After adding features, changing installation, updating docs

**Pattern:**
```
chore: update README with tab completion docs
```

**Use Cases:**
- Adding new command documentation
- Updating installation instructions
- Fixing typos/formatting
- Adding badges or links

**When NOT to use chore:**
```
❌ Adding new API docs        → docs: add API documentation
❌ Adding architecture guide  → docs: add architecture guide
```

**Process:**
```bash
git add README.md
git commit -m "chore: update README with new features"
```

### 4. Dependency Updates

**When:**
- Security updates
- Regular maintenance
- New tool requirements

**Pattern:**
```
chore: update dependencies
chore: update docker to 7.2.0
chore: add pytest-cov dependency
```

**Files Changed:**
- `pyproject.toml` - Dependencies
- `uv.lock` - Lock file

**Process:**
```bash
# Update specific package
uv sync --upgrade-package docker

# Or update all
uv sync --upgrade

# Commit
git add pyproject.toml uv.lock
git commit -m "chore: update docker to 7.2.0"
```

**Categories:**

**Security updates:**
```
chore: update docker for security fix CVE-2024-XXXX
```

**Feature dependencies:**
```
chore: add pytest-cov for coverage reporting
```

**Version bumps:**
```
chore: update dependencies to latest versions
```

### 5. Tooling Configuration

**When:** Configuring development tools

**Examples:**
```
chore: configure pytest and coverage
chore: add black formatter configuration
chore: update .gitignore for coverage files
chore: add pre-commit hooks
chore: configure ruff linter
```

**Files:**
- `pyproject.toml` - Tool configuration
- `.gitignore` - Ignore patterns
- `.pre-commit-config.yaml` - Pre-commit hooks
- `.editorconfig` - Editor settings

**Process:**
```bash
git add pyproject.toml .gitignore
git commit -m "chore: configure pytest and coverage"
```

### 6. Build Configuration

**When:** Changing build setup

**Examples:**
```
chore: update build configuration
chore: add setuptools configuration
chore: configure uv build
```

**Files:**
- `pyproject.toml` - Build configuration
- Build scripts

### 7. Repository Maintenance

**When:** Repository cleanup and organization

**Examples:**
```
chore: clean up unused files
chore: organize project structure
chore: remove deprecated code
chore: update license year
chore: add code of conduct
chore: update contributor guide
```

**Process:**
```bash
git rm deprecated_file.py
git commit -m "chore: remove deprecated files"
```

### 8. Git Configuration

**When:** Updating git-related files

**Examples:**
```
chore: update .gitignore for coverage files
chore: add .gitattributes for line endings
chore: configure .mailmap
```

## Release Chore Workflow

Most chores happen during the release process. Here's the standard workflow:

### Traditional Approach (Multiple Commits)

```bash
# 1. Bump version
# Edit pyproject.toml: version = "0.9.2"
# Edit src/caffeinated_whale_cli/__init__.py: __version__ = "0.9.2"
git commit -m "chore: bump version to 0.9.2"

# 2. Update CHANGELOG
# Edit CHANGELOG.md with release notes
git commit -m "chore: update CHANGELOG for v0.9.2"

# 3. Update README
# Edit README.md with new features
git commit -m "chore: update README with new features"

# 4. Create tag
git tag v0.9.2

# 5. Push
git push origin develop --tags
```

**Commits Generated:** 3 chore commits

**Observed in Project:** This is the current pattern (creates multiple chore commits).

### Recommended Approach (Single Commit)

```bash
# 1. Update all release files
# - version in pyproject.toml
# - __version__ in src/caffeinated_whale_cli/__init__.py
# - CHANGELOG.md
# - README.md (if needed)

# 2. Single commit
git commit -m "chore: release v0.9.2

- Bump version to 0.9.2
- Update CHANGELOG with new features
- Update README with tab completion docs"

# 3. Create tag
git tag v0.9.2

# 4. Push
git push origin develop --tags
```

**Commits Generated:** 1 chore commit

**Benefits:**
- Cleaner commit history
- Atomic release preparation
- Easier to revert if needed
- Follows atomic commit principle

## Chore Checklist

Before committing a chore:

- [ ] Is this truly a chore? (Not a feat, fix, docs, etc.)
- [ ] Is the commit message clear and specific?
- [ ] Does it use `chore:` prefix (not `update:` or `bump:`)?
- [ ] Are all related files included?
- [ ] Has the lock file been updated (if dependencies changed)?
- [ ] Is the change documented (if needed)?

## Chore Anti-Patterns

### 1. Vague Messages

```
❌ chore: updates              # What was updated?
❌ chore: changes              # What changed?
❌ chore: maintenance          # What maintenance?
```

**Better:**
```
✅ chore: update dependencies to latest versions
✅ chore: update README with installation instructions
✅ chore: clean up deprecated utility functions
```

### 2. Wrong Type Prefix

```
❌ update: CHANGELOG           # Should be chore:
❌ bump: version               # Should be chore:
❌ add: ci and docs            # Should be feat: or docs:
```

### 3. Mixed Changes

```
❌ chore: update README and fix bug
   # This mixes chore and fix - split into 2 commits
```

**Better:**
```
✅ fix: port conflict detection issue
✅ chore: update README with port detection docs
```

### 4. Incorrect Categorization

```
❌ chore: add new feature     # This is feat:
❌ chore: fix broken tests    # This is fix: or test:
❌ chore: add API docs        # This is docs:
```

## Chore vs. Other Types

### Chore vs. Docs

**Use `docs:`** when adding substantial documentation:
```
docs: add testing guide
docs: add API reference
docs: add architecture overview
```

**Use `chore:`** for documentation updates:
```
chore: update README with new features
chore: fix typos in README
chore: update installation instructions
```

**Rule of Thumb:** New docs = `docs:`, updating existing = `chore:`

### Chore vs. Refactor

**Use `refactor:`** when changing code structure:
```
refactor: extract port utilities
refactor: simplify Docker client creation
```

**Use `chore:`** when cleaning up without structural changes:
```
chore: remove unused imports
chore: clean up deprecated code
```

**Rule of Thumb:** Code reorganization = `refactor:`, cleanup = `chore:`

### Chore vs. Build

**Use `build:`** for build system changes:
```
build: add Docker build configuration
build: update CI/CD pipeline
build: configure webpack
```

**Use `chore:`** for simple build config updates:
```
chore: update pyproject.toml metadata
chore: add black configuration
```

**Rule of Thumb:** Build system = `build:`, tool config = `chore:`

### Chore vs. CI

**Use `ci:`** for CI/CD workflow changes:
```
ci: add GitHub Actions workflow
ci: update test pipeline
ci: add deployment job
```

**Use `chore:`** for simple CI updates:
```
chore: update CI badge in README
chore: fix CI configuration typo
```

**Rule of Thumb:** Workflow changes = `ci:`, metadata = `chore:`

## Automation Opportunities

### 1. Version Bumping

Use tools to automate version bumping:

```bash
# Using bump2version
bump2version patch  # 0.9.1 → 0.9.2
bump2version minor  # 0.9.2 → 0.10.0
bump2version major  # 0.10.0 → 1.0.0

# Automatically updates:
# - pyproject.toml
# - __init__.py
# - Creates git commit and tag
```

### 2. CHANGELOG Generation

Use conventional-changelog:

```bash
# Generate CHANGELOG from commits
npx conventional-changelog -p angular -i CHANGELOG.md -s

# Updates CHANGELOG.md based on commit messages
```

### 3. Release Automation

Use release-it or similar:

```bash
# Interactive release
npx release-it

# Handles:
# - Version bump
# - CHANGELOG update
# - Git commit
# - Git tag
# - GitHub release
```

## Best Practices

### 1. Batch Related Chores

Instead of:
```
❌ chore: update dependency 1
❌ chore: update dependency 2
❌ chore: update dependency 3
```

Do:
```
✅ chore: update dependencies

- Update docker to 7.2.0
- Update typer to 0.17.0
- Update rich to 14.1.0
```

### 2. Be Specific

Instead of:
```
❌ chore: update README
```

Do:
```
✅ chore: update README with tab completion docs
```

### 3. Include Version Numbers

Instead of:
```
❌ chore: bump version
```

Do:
```
✅ chore: bump version to 0.9.2
```

### 4. Separate Concerns

Don't mix chores with features/fixes:
```
❌ Bad:
   - Add new feature
   - Update README
   - Bump version
   (This is 3 separate commits)

✅ Good:
   Commit 1: feat: add tab completion
   Commit 2: chore: update README with tab completion docs
   Commit 3: chore: bump version to 0.9.2
```

## Chore Frequency

### Based on Project Analysis

**High Frequency (Every Release):**
- Version bumps
- CHANGELOG updates
- README updates

**Medium Frequency (Monthly):**
- Dependency updates
- Tool configuration updates

**Low Frequency (As Needed):**
- Repository reorganization
- License updates
- .gitignore updates

### Recommended Schedule

**Every Release:**
- [ ] Bump version
- [ ] Update CHANGELOG
- [ ] Update README (if features added)

**Monthly:**
- [ ] Check for dependency updates
- [ ] Review and clean up old branches
- [ ] Update documentation links

**Quarterly:**
- [ ] Major dependency updates
- [ ] Review and update tooling
- [ ] Clean up deprecated code

**Annually:**
- [ ] Update license year
- [ ] Review contributor guide
- [ ] Update copyright notices

## Summary

### Quick Decision Tree

```
Does it change code behavior?
  Yes → Not a chore (feat/fix/refactor)
  No → Continue

Is it documentation?
  Substantial new docs → docs:
  Updating existing docs → chore:

Is it build/CI related?
  Workflow changes → build:/ci:
  Config updates → chore:

Is it testing?
  New tests → test:
  Test config → chore:

Otherwise → chore:
```

### Chore Type Reference

| Task | Type | Example |
|------|------|---------|
| Version bump | `chore:` | `chore: bump version to 0.9.2` |
| CHANGELOG update | `chore:` | `chore: update CHANGELOG for v0.9.2` |
| README update | `chore:` | `chore: update README with new features` |
| Dependency update | `chore:` | `chore: update docker to 7.2.0` |
| Add dependency | `chore:` | `chore: add pytest-cov dependency` |
| Tool configuration | `chore:` | `chore: configure black formatter` |
| .gitignore update | `chore:` | `chore: add coverage files to .gitignore` |
| Cleanup | `chore:` | `chore: remove deprecated files` |
| License update | `chore:` | `chore: update license year to 2025` |

## Resources

- [Conventional Commits](https://www.conventionalcommits.org/)
- [Semantic Versioning](https://semver.org/)
- [Keep a Changelog](https://keepachangelog.com/)
- [bump2version](https://github.com/c4urself/bump2version)
- [conventional-changelog](https://github.com/conventional-changelog/conventional-changelog)
- [release-it](https://github.com/release-it/release-it)
