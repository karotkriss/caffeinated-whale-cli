# Git Workflow Guide

## Overview

This is the master guide for Git workflows in the caffeinated-whale-cli project. It brings together commit messages, branch naming, and chore management into a cohesive workflow.

## Quick Links

- [Commit Message Guide](./commit-messages.md) - How to write commit messages
- [Branch Naming Guide](./branch-naming.md) - How to name branches
- [Chores Guide](./chores.md) - Managing maintenance tasks

## Project Analysis Summary

Based on analysis of 79 commits:

### Commit Type Distribution

| Type | Count | Percentage | Most Common Use |
|------|-------|------------|-----------------|
| `chore:` | 29 | 36.7% | Version bumps, CHANGELOG updates |
| `fix:` | 24 | 30.4% | Bug fixes |
| `feat:` | 16 | 20.3% | New features |
| Other | 10 | 12.6% | Merges, refactors, etc. |

### Key Insights

1. **Chores dominate** - Most commits are maintenance (version, CHANGELOG, README)
2. **Bug fixes are frequent** - Active development with continuous improvement
3. **Feature development is steady** - New features added regularly
4. **Consistent patterns** - Most commits follow conventional format

### Current Branches

```
develop              # Main development branch
feat-tab-completion  # Tab completion feature
init                 # Initialization feature
ports                # Port conflict detection
```

**Observation:** Branch naming is mostly consistent with `feat-` prefix.

## Standard Workflow

### 1. Starting New Work

```bash
# Update develop
git checkout develop
git pull origin develop

# Create feature branch
git checkout -b feat-my-feature

# Work on feature
# ... make changes ...

# Commit with conventional format
git add .
git commit -m "feat: add my feature

Detailed description of what the feature does.

- Key point 1
- Key point 2"

# Push to remote
git push -u origin feat-my-feature
```

### 2. During Development

```bash
# Make changes
git add .

# Commit atomically (one logical change per commit)
git commit -m "feat: add completion caching"
git commit -m "test: add cache expiry tests"
git commit -m "docs: update README with caching info"

# Push regularly
git push
```

### 3. Before Pull Request

```bash
# Update from develop
git checkout develop
git pull origin develop
git checkout feat-my-feature
git rebase develop

# Run tests
uv run pytest

# Run linting
uv run black src/
uv run ruff check src/

# Push (force push if rebased)
git push --force-with-lease
```

### 4. Pull Request

**Title Format:**
```
feat: add tab completion (#PR_NUMBER)
```

**Description Template:**
```markdown
## Summary
Brief description of what this PR does.

## Changes
- Change 1
- Change 2
- Change 3

## Testing
- [ ] Tests added/updated
- [ ] All tests pass
- [ ] Linting passes

## Documentation
- [ ] README updated (if needed)
- [ ] Code comments added

## Related Issues
Closes #42
See also #40
```

### 5. After Merge

```bash
# Update local develop
git checkout develop
git pull origin develop

# Delete feature branch
git branch -d feat-my-feature
git push origin --delete feat-my-feature
```

## Release Workflow

### Traditional Approach (Current Pattern)

```bash
# 1. Bump version
# Edit pyproject.toml: version = "0.9.2"
# Edit src/caffeinated_whale_cli/__init__.py: __version__ = "0.9.2"
git add pyproject.toml src/caffeinated_whale_cli/__init__.py
git commit -m "chore: bump version to 0.9.2"

# 2. Update CHANGELOG
# Edit CHANGELOG.md
git add CHANGELOG.md
git commit -m "chore: update CHANGELOG for v0.9.2"

# 3. Update README (if needed)
# Edit README.md
git add README.md
git commit -m "chore: update README with new features"

# 4. Create tag
git tag v0.9.2

# 5. Push
git push origin develop --tags
```

**Result:** 3 separate chore commits

### Recommended Approach (Atomic Releases)

```bash
# 1. Update all release files
# - pyproject.toml (version)
# - src/caffeinated_whale_cli/__init__.py (__version__)
# - CHANGELOG.md (release notes)
# - README.md (if needed)

# 2. Single atomic commit
git add pyproject.toml src/caffeinated_whale_cli/__init__.py CHANGELOG.md README.md
git commit -m "chore: release v0.9.2

- Bump version to 0.9.2
- Update CHANGELOG with new features and fixes
- Update README with tab completion documentation"

# 3. Create and push tag
git tag v0.9.2
git push origin develop --tags
```

**Result:** 1 atomic chore commit (cleaner history)

## Commit Message Examples

### Feature Commits

**Simple:**
```
feat: tab completion
```

**With Details:**
```
feat: tab completion

Add intelligent tab completion for CLI commands.

- Project names from Docker
- App names from cache
- Site names from cache
- 2-second TTL caching

Supports Bash, Zsh, Fish, PowerShell
```

### Bug Fix Commits

**Simple:**
```
fix: ctrl + c loop in start
```

**With Context:**
```
fix: ctrl + c loop in start

Prevent infinite loop when user interrupts start command.

Previously, pressing Ctrl+C would cause the prompt to
re-appear in a loop. Now exits gracefully on interrupt.

Fixes #123
```

### Chore Commits

**Version Bump:**
```
chore: bump version to 0.9.2
```

**CHANGELOG:**
```
chore: update CHANGELOG for v0.9.2
```

**Dependencies:**
```
chore: update dependencies

- Update docker to 7.2.0
- Update typer to 0.17.0
- Update rich to 14.1.0
```

### Test Commits

```
test: add tests for tab completion

- 27 tests across 6 test classes
- 92% code coverage
- Tests caching, error handling, context-awareness
```

### Documentation Commits

**New Docs:**
```
docs: add testing guide

Complete guide covering:
- Running tests
- Writing new tests
- Coverage goals
- Best practices
```

**README Update:**
```
chore: update README with tab completion docs

Add installation and usage instructions for tab completion.
```

## Branch Naming Examples

### Feature Branches

```
feat-tab-completion           ✅ Good: clear and concise
feat-port-detection          ✅ Good: descriptive
feat-unlock-command          ✅ Good: specific
feat-bench-status            ✅ Good: clear purpose

feat-improvements            ❌ Bad: vague
feature-tab-completion       ❌ Bad: use "feat" not "feature"
tab-completion               ❌ Bad: missing type prefix
```

### Fix Branches

```
fix-ctrl-c-loop              ✅ Good: describes issue
fix-port-conflicts           ✅ Good: clear problem
fix-cache-invalidation       ✅ Good: specific

fix-bugs                     ❌ Bad: too vague
bugfix-port-conflicts        ❌ Bad: use "fix" not "bugfix"
ctrl-c-loop                  ❌ Bad: missing type prefix
```

### Chore Branches

```
chore-update-deps            ✅ Good: clear purpose
chore-configure-ci           ✅ Good: specific task

chore-stuff                  ❌ Bad: vague
update-deps                  ❌ Bad: missing type prefix
```

## Common Scenarios

### Scenario 1: Adding a New Feature

```bash
# 1. Create branch
git checkout -b feat-export-data

# 2. Implement feature
# ... code changes ...
git commit -m "feat: add export data command

Allows users to export project data to JSON/CSV formats."

# 3. Add tests
# ... write tests ...
git commit -m "test: add export command tests"

# 4. Update docs
# ... update README ...
git commit -m "chore: update README with export command"

# 5. Create PR
# ... via GitHub UI ...
```

**Commits:** 3 (feat, test, chore)

### Scenario 2: Fixing a Bug

```bash
# 1. Create branch
git checkout -b fix-memory-leak

# 2. Fix bug
# ... code changes ...
git commit -m "fix: memory leak in cache system

Release cached Docker client on completion.
Prevents memory accumulation during long sessions.

Fixes #156"

# 3. Add regression test
# ... write test ...
git commit -m "test: add cache memory leak test"

# 4. Create PR
```

**Commits:** 2 (fix, test)

### Scenario 3: Refactoring

```bash
# 1. Create branch
git checkout -b refactor-docker-client

# 2. Refactor code
# ... restructure ...
git commit -m "refactor: extract Docker client creation

Move Docker client creation to utility function.
No behavior changes."

# 3. Update tests (if needed)
git commit -m "test: update Docker client tests"

# 4. Create PR
```

**Commits:** 2 (refactor, test)

### Scenario 4: Preparing a Release

**Option A: Traditional (Multiple Commits)**
```bash
# On develop branch
# Update pyproject.toml and src/caffeinated_whale_cli/__init__.py
git commit -m "chore: bump version to 0.9.2"
git commit -m "chore: update CHANGELOG for v0.9.2"
git commit -m "chore: update README with new features"
git tag v0.9.2
git push origin develop --tags
```

**Option B: Atomic (Single Commit)**
```bash
# On develop branch
# Update pyproject.toml, __init__.py, CHANGELOG.md, README.md
git commit -m "chore: release v0.9.2

- Bump version to 0.9.2 (pyproject.toml + __init__.py)
- Update CHANGELOG with tab completion feature
- Update README with installation and usage"

git tag v0.9.2
git push origin develop --tags
```

**Recommendation:** Use Option B for cleaner history.

## Code Review Checklist

### For Reviewers

- [ ] Commit messages follow conventional format
- [ ] Branch name matches commit types
- [ ] Each commit is atomic (one logical change)
- [ ] Tests are included/updated
- [ ] Documentation is updated
- [ ] Code follows style guide
- [ ] No merge conflicts

### For Authors

Before requesting review:

- [ ] All commits have proper type prefix
- [ ] Commit messages are descriptive
- [ ] Branch is up to date with develop
- [ ] Tests pass locally
- [ ] Linting passes
- [ ] README updated (if needed)
- [ ] CHANGELOG updated (if release)

## Git Hooks

### Pre-commit Hook

Create `.git/hooks/pre-commit`:

```bash
#!/bin/sh

# Run black
uv run black src/ tests/ --check
if [ $? -ne 0 ]; then
    echo "Black formatting failed. Run: uv run black src/ tests/"
    exit 1
fi

# Run ruff
uv run ruff check src/
if [ $? -ne 0 ]; then
    echo "Ruff linting failed. Run: uv run ruff check src/ --fix"
    exit 1
fi

# Run tests
uv run pytest
if [ $? -ne 0 ]; then
    echo "Tests failed."
    exit 1
fi
```

### Commit-msg Hook

Create `.git/hooks/commit-msg`:

```bash
#!/bin/sh

commit_msg=$(cat "$1")

# Check for conventional commit format
if ! echo "$commit_msg" | grep -qE "^(feat|fix|chore|docs|test|refactor|perf|style|build|ci|revert): .+"; then
    echo "Error: Commit message must follow conventional format:"
    echo "  <type>: <description>"
    echo ""
    echo "Types: feat, fix, chore, docs, test, refactor, perf, style, build, ci, revert"
    echo ""
    echo "Example: feat: add tab completion"
    exit 1
fi
```

Make executable:
```bash
chmod +x .git/hooks/pre-commit
chmod +x .git/hooks/commit-msg
```

## Automation Tools

### Commitlint

```bash
# Install
npm install --save-dev @commitlint/{cli,config-conventional}

# Configure
echo "module.exports = {extends: ['@commitlint/config-conventional']}" > commitlint.config.js
```

### Husky

```bash
# Install
npm install --save-dev husky

# Setup
npx husky install
npx husky add .husky/commit-msg 'npx --no -- commitlint --edit $1'
npx husky add .husky/pre-commit 'uv run pytest && uv run black src/ --check'
```

### Release Automation

```bash
# Using release-it
npm install --save-dev release-it

# Configure .release-it.json
{
  "git": {
    "commitMessage": "chore: release v${version}",
    "tagName": "v${version}"
  },
  "github": {
    "release": true
  },
  "npm": {
    "publish": false
  }
}

# Run release
npx release-it
```

## Tips and Tricks

### Amending Commits

```bash
# Fix last commit message
git commit --amend -m "feat: correct commit message"

# Add forgotten files
git add forgotten_file.py
git commit --amend --no-edit
```

### Interactive Rebase

```bash
# Clean up last 3 commits
git rebase -i HEAD~3

# Options:
# pick   - keep commit
# reword - change message
# edit   - amend commit
# squash - combine with previous
# drop   - remove commit
```

### Commit Templates

```bash
# Create template
cat > ~/.gitmessage << 'EOF'
# <type>: <subject>
#
# [body]
#
# [footer]
#
# Types: feat, fix, chore, docs, test, refactor, perf, style, build, ci
EOF

# Use template
git config --global commit.template ~/.gitmessage
```

### Searching History

```bash
# Find commits by message
git log --grep="tab completion"

# Find commits by author
git log --author="Christopher"

# Find commits by file
git log -- src/completion_utils.py

# Find commits by type
git log --grep="^feat:"
```

## Troubleshooting

### Wrong Commit Type

```bash
# Last commit has wrong type
git commit --amend -m "fix: correct type prefix"
```

### Forgot to Create Branch

```bash
# Made commits on develop
git branch feat-my-feature  # Create branch at current commit
git reset --hard origin/develop  # Reset develop
git checkout feat-my-feature  # Switch to feature branch
```

### Need to Split Commit

```bash
# Reset last commit but keep changes
git reset --soft HEAD~1

# Stage and commit separately
git add file1.py
git commit -m "feat: add feature"

git add file2.py
git commit -m "test: add tests"
```

## Best Practices Summary

### Commits

1. Use conventional commit format
2. One logical change per commit
3. Write descriptive messages
4. Use imperative mood
5. Include context in body
6. Reference issues in footer

### Branches

1. Use type prefixes (feat-, fix-, etc.)
2. Keep names short and descriptive
3. Use kebab-case
4. Delete after merge
5. Stay up to date with develop

### Workflow

1. Work in feature branches
2. Commit frequently
3. Test before pushing
4. Rebase before PR
5. Squash if needed
6. Clean up after merge

## Resources

- [Commit Message Guide](./commit-messages.md)
- [Branch Naming Guide](./branch-naming.md)
- [Chores Guide](./chores.md)
- [Testing Guide](../testing/guide.md)
- [Conventional Commits](https://www.conventionalcommits.org/)
- [Git Flow](https://nvie.com/posts/a-successful-git-branching-model/)
- [GitHub Flow](https://guides.github.com/introduction/flow/)
