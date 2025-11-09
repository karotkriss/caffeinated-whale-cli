# Branch Naming Guide

## Overview

This guide defines branch naming conventions for the caffeinated-whale-cli project based on observed patterns and industry best practices.

## Format

```
<type>[-<ticket>]-<description>
```

### Components

- **type**: Category of changes (required)
- **ticket**: Issue/ticket number (optional)
- **description**: Short kebab-case description (required)

### Examples

```
feat-tab-completion
fix-port-conflict-detection
chore-update-dependencies
docs-testing-guide
refactor-docker-utils
```

With ticket numbers:

```
feat-42-tab-completion
fix-123-port-conflicts
chore-99-bump-version
```

## Branch Types

### Primary Types

| Type | Purpose | When to Use | Example |
|------|---------|-------------|---------|
| **feat** | New feature | Adding new functionality | `feat-tab-completion` |
| **fix** | Bug fix | Fixing bugs | `fix-ctrl-c-loop` |
| **chore** | Maintenance | Dependencies, tooling, cleanup | `chore-update-deps` |
| **docs** | Documentation | Adding or updating docs | `docs-testing-guide` |
| **refactor** | Code restructure | Improving code without changing behavior | `refactor-port-utils` |
| **test** | Testing | Adding or updating tests | `test-completion-utils` |
| **perf** | Performance | Performance improvements | `perf-cache-optimization` |
| **style** | Code style | Formatting, linting | `style-black-formatting` |
| **build** | Build system | Build configuration changes | `build-docker-setup` |
| **ci** | CI/CD | Workflow changes | `ci-github-actions` |

### Special Branches

| Branch | Purpose | Lifetime |
|--------|---------|----------|
| **main** or **master** | Production-ready code | Permanent |
| **develop** | Integration branch | Permanent |
| **release-v0.9.1** | Release preparation | Temporary |
| **hotfix-critical-bug** | Emergency production fixes | Temporary |

## Current Project Branches

### Analysis of Existing Branches

From the repository:

```
main/master          # Production branch
develop              # Main development branch
feat-tab-completion  # Feature branch (tab completion)
feat-alias-bench-instances-retroactively
feat-bench-aliasing
feat-bench-list-apps-command
feat-bench-status
feat-run-bench-commands-with-cwcli
init                 # Special: initialization branch
ports                # Feature: port conflict detection
```

### Pattern Observations

✅ **Good patterns:**
- `feat-tab-completion` - Clear, descriptive
- `feat-bench-status` - Type + description
- `ports` - Simple and clear (but missing type prefix)

⚠️ **Inconsistent patterns:**
- `init` - Missing type prefix (should be `feat-init` or `chore-init`)
- `ports` - Missing type prefix (should be `feat-ports`)
- Long names like `feat-alias-bench-instances-retroactively` (consider shortening)

## Naming Rules

### ✅ Do

- Use lowercase
- Use hyphens to separate words (kebab-case)
- Start with type prefix
- Be descriptive but concise
- Use present tense
- Use singular nouns when applicable

**Good Examples:**
```
feat-tab-completion
feat-port-detection
fix-cache-invalidation
chore-bump-version
docs-api-reference
refactor-docker-client
test-port-utils
perf-query-optimization
```

### ❌ Don't

- Don't use underscores: ~~`feat_tab_completion`~~
- Don't use camelCase: ~~`featTabCompletion`~~
- Don't use capitals: ~~`Feat-Tab-Completion`~~
- Don't use spaces: ~~`feat tab completion`~~
- Don't be vague: ~~`feat-updates`~~ or ~~`fix-bugs`~~
- Don't use past tense: ~~`feat-added-completion`~~

**Bad Examples:**
```
❌ feat_tab_completion           # Underscores
❌ featTabCompletion             # CamelCase
❌ Feat-Tab-Completion           # Capitals
❌ tab-completion                # Missing type
❌ feat-updates                  # Vague
❌ feat-added-completion         # Past tense
❌ feature/tab-completion        # Wrong separator (use hyphen)
```

## Branch Naming Patterns

### Feature Branches

Pattern: `feat-<description>` or `feat-<ticket>-<description>`

```
feat-tab-completion
feat-unlock-command
feat-update-command
feat-bench-status
feat-42-user-auth
feat-123-export-data
```

### Bug Fix Branches

Pattern: `fix-<description>` or `fix-<ticket>-<description>`

```
fix-ctrl-c-loop
fix-port-conflicts
fix-cache-invalidation
fix-docker-connection
fix-42-login-error
fix-123-memory-leak
```

### Chore Branches

Pattern: `chore-<description>`

```
chore-update-deps
chore-bump-version
chore-cleanup-logs
chore-configure-ci
chore-update-readme
```

### Documentation Branches

Pattern: `docs-<description>`

```
docs-testing-guide
docs-api-reference
docs-commit-guide
docs-readme-update
docs-installation
```

### Refactor Branches

Pattern: `refactor-<description>`

```
refactor-port-utils
refactor-docker-client
refactor-cache-system
refactor-command-structure
```

### Release Branches

Pattern: `release-v<version>` or `release/<version>`

```
release-v0.9.1
release-v1.0.0
release-v1.2.0-beta
```

### Hotfix Branches

Pattern: `hotfix-<description>` or `hotfix-v<version>-<description>`

```
hotfix-critical-security
hotfix-v0.9.1-docker-crash
hotfix-memory-leak
```

## Branch Lifecycle

### Feature Branch Workflow

```bash
# 1. Create from develop
git checkout develop
git pull origin develop
git checkout -b feat-tab-completion

# 2. Make changes and commit
git add .
git commit -m "feat: add tab completion"

# 3. Push to remote
git push -u origin feat-tab-completion

# 4. Create pull request
# (via GitHub UI)

# 5. Merge to develop
# (via pull request)

# 6. Delete branch after merge
git branch -d feat-tab-completion
git push origin --delete feat-tab-completion
```

### Hotfix Branch Workflow

```bash
# 1. Create from main
git checkout main
git pull origin main
git checkout -b hotfix-critical-bug

# 2. Fix and commit
git commit -m "fix: critical security issue"

# 3. Merge to main
git checkout main
git merge hotfix-critical-bug

# 4. Tag release
git tag v0.9.2
git push origin main --tags

# 5. Merge to develop
git checkout develop
git merge hotfix-critical-bug

# 6. Delete branch
git branch -d hotfix-critical-bug
```

## Branching Strategy

### Git Flow (Recommended)

```
main/master
  ├── release-v1.0.0
  └── develop
      ├── feat-tab-completion
      ├── feat-port-detection
      ├── fix-cache-bug
      └── chore-update-deps
```

**Branches:**
- `main` - Production code
- `develop` - Integration branch
- `feat-*` - Features (from develop)
- `fix-*` - Bug fixes (from develop)
- `release-*` - Release prep (from develop)
- `hotfix-*` - Critical fixes (from main)

### Simplified Flow (Current Project)

```
develop
  ├── feat-tab-completion
  ├── feat-ports
  └── init
```

**Current pattern:** Features branch from `develop`, merge via PR.

## Branch Description Guidelines

### Length

- **Ideal:** 2-4 words
- **Maximum:** 50 characters
- **If longer:** Consider if it's too complex (maybe split the feature)

### Clarity

Be specific about what the branch does:

**Good:**
```
feat-tab-completion           # Clear: adds tab completion
fix-port-conflict             # Clear: fixes port conflicts
chore-update-docker-deps      # Clear: updates Docker dependencies
```

**Bad:**
```
feat-improvements             # Vague: what improvements?
fix-bugs                      # Vague: which bugs?
chore-updates                 # Vague: updates to what?
```

## Long Branch Names

If branch name gets too long, consider:

### Before (Too Long)
```
feat-alias-bench-instances-retroactively
```

### After (Better)
```
feat-bench-alias              # Simplified
feat-retroactive-aliasing     # Focus on key aspect
feat-bench-name-persistence   # More descriptive
```

### Strategy
1. Remove redundant words
2. Use abbreviations (carefully)
3. Focus on the main feature
4. Consider splitting if truly complex

## Ticket/Issue Integration

### With GitHub Issues

```
feat-42-tab-completion        # Issue #42
fix-123-port-conflicts        # Issue #123
chore-99-bump-version         # Issue #99
```

### With Jira/Other Tools

```
feat-PROJ-42-tab-completion
fix-CWCLI-123-port-bug
chore-MAINT-99-dependencies
```

## Branch Prefixes by Workflow

### GitHub Flow

```
<type>-<description>
```

Simple, flat structure:
```
feat-tab-completion
fix-port-conflicts
docs-readme
```

### Git Flow

```
feature/<description>
bugfix/<description>
hotfix/<description>
release/<version>
```

Nested structure:
```
feature/tab-completion
bugfix/port-conflicts
hotfix/critical-security
release/v1.0.0
```

**Project uses:** GitHub Flow style (flat with type prefixes)

## Special Cases

### Multiple Related Features

If working on multiple related features:

```
feat-ports                    # Parent/umbrella
feat-ports-detection          # Specific aspect
feat-ports-resolution         # Specific aspect
```

Or use sequential branches:
```
feat-tab-completion-v1
feat-tab-completion-v2
feat-tab-completion-enhanced
```

### Experimental Branches

```
experiment-new-cache-strategy
spike-docker-alternative
poc-graphql-api
```

Prefix: `experiment-`, `spike-`, or `poc-` (proof of concept)

### Personal/WIP Branches

```
wip-tab-completion
draft-port-detection
username-feat-experiment
```

Prefix: `wip-` (work in progress) or username

## Branch Protection Rules

### Protected Branches

Configure in GitHub Settings > Branches:

**For `main`:**
- Require pull request reviews (1-2 reviewers)
- Require status checks to pass
- Require branches to be up to date
- No direct pushes
- No force pushes
- No deletions

**For `develop`:**
- Require pull request reviews (optional)
- Require status checks to pass
- Allow force pushes (with caution)

## Cleanup

### Delete Merged Branches

```bash
# Delete local branch
git branch -d feat-tab-completion

# Delete remote branch
git push origin --delete feat-tab-completion

# Prune deleted remote branches
git fetch --prune
```

### Automated Cleanup

GitHub Settings > General > Pull Requests:
- ✅ Automatically delete head branches

## Migration Guide

### Updating Existing Branches

If you have branches without type prefixes:

```bash
# Rename local branch
git branch -m ports feat-ports
git branch -m init feat-init

# Delete old remote branch
git push origin --delete ports

# Push renamed branch
git push -u origin feat-ports
```

**Note:** Communicate with team before renaming pushed branches!

## Quick Reference

### Branch Type Decision Tree

```
New feature?              → feat-<description>
Bug fix?                  → fix-<description>
Maintenance/tooling?      → chore-<description>
Documentation?            → docs-<description>
Code restructure?         → refactor-<description>
Adding tests?             → test-<description>
Performance improvement?  → perf-<description>
Release preparation?      → release-v<version>
Critical production fix?  → hotfix-<description>
```

### Naming Checklist

Before creating a branch:

- [ ] Starts with type prefix
- [ ] Uses kebab-case (hyphens)
- [ ] Is lowercase
- [ ] Is descriptive (2-4 words)
- [ ] Under 50 characters
- [ ] Matches commit type that will be used
- [ ] Doesn't use past tense
- [ ] Doesn't include redundant words

## Common Patterns

### From Project History

**Current good patterns:**
```
✅ feat-tab-completion
✅ feat-bench-status
✅ feat-run-bench-commands-with-cwcli
```

**Could be improved:**
```
⚠️ ports → feat-ports or feat-port-detection
⚠️ init → feat-init or chore-init-project
```

**Too long (consider shortening):**
```
⚠️ feat-alias-bench-instances-retroactively
   → feat-bench-aliasing or feat-retroactive-aliases

⚠️ feat-run-bench-commands-with-cwcli
   → feat-bench-commands or feat-run-command
```

## Resources

- [Git Flow](https://nvie.com/posts/a-successful-git-branching-model/)
- [GitHub Flow](https://guides.github.com/introduction/flow/)
- [Branch Naming Conventions](https://deepsource.io/blog/git-branch-naming-conventions/)
- [Trunk Based Development](https://trunkbaseddevelopment.com/)
