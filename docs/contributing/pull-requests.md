# Pull Request Guidelines

This guide covers how to create, name, and manage pull requests (PRs) for caffeinated-whale-cli.

## Overview

Pull requests are how we merge code from feature branches into the main development branch. Good PR titles and descriptions help reviewers understand your changes quickly and make the project history more readable.

---

## PR Title Format

PR titles should follow the same format as commit messages:

```
<type>: <description>
```

### Types

Use the same types as [commit messages](./commit-messages.md):

| Type | When to Use | Example |
|------|-------------|---------|
| `feat:` | New features | `feat: add tab completion for project names` |
| `fix:` | Bug fixes | `fix: port conflict detection in mixed scenarios` |
| `chore:` | Maintenance tasks | `chore: bump version to 0.10.0` |
| `docs:` | Documentation only | `docs: add pull request guidelines` |
| `test:` | Adding or updating tests | `test: add port utilities test suite` |
| `refactor:` | Code restructuring | `refactor: extract port utils from commands` |
| `perf:` | Performance improvements | `perf: optimize project scanning` |
| `style:` | Code formatting | `style: format code with black` |
| `build:` | Build system changes | `build: update dependencies` |
| `ci:` | CI/CD changes | `ci: add lint workflow` |

### Description Guidelines

**✅ Good PR titles:**
```
feat: add tab completion support
fix: ctrl + c loop in start command
docs: restructure documentation into categories
refactor: consolidate CI/CD documentation
```

**❌ Bad PR titles:**
```
Update stuff                    # Too vague
feat add tab completion         # Missing colon
Fixed bugs                      # Not specific, wrong tense
WIP: Working on feature         # Don't use WIP prefix
```

### Rules

1. **Use imperative mood** - "add" not "added", "fix" not "fixed"
2. **Be specific but concise** - Aim for 50 characters or less
3. **No period at the end** - PR titles are not sentences
4. **Lowercase after colon** - `feat: add` not `feat: Add`
5. **One primary type** - If the PR does multiple things, use the most significant type

---

## PR Description

A good PR description helps reviewers understand the context and scope of changes.

### Template

```markdown
## Summary
Brief description of what this PR does (1-3 sentences).

## Changes
- Change 1
- Change 2
- Change 3

## Why
Explain the motivation for these changes:
- Problem being solved
- Feature being added
- Technical debt being addressed

## Testing
- [ ] All tests pass (`uv run pytest`)
- [ ] Code is formatted (`uv run black src/ tests/`)
- [ ] Linting passes (`uv run ruff check src/`)
- [ ] Manual testing performed (describe what you tested)

## Documentation
- [ ] README updated (if needed)
- [ ] CHANGELOG updated
- [ ] Code comments added
- [ ] Docstrings updated

## Related Issues
Closes #XX
Fixes #YY
See also #ZZ
```

### Example PR Description

```markdown
## Summary
Adds intelligent tab completion for project names, app names, and site names across all commands and shells.

## Changes
- Implement completion_utils.py with context-aware completion functions
- Add shell completion registration via Typer
- Cache completion results with 2-second TTL for performance
- Support Bash, Zsh, Fish, and PowerShell

## Why
Tab completion significantly improves the developer experience by:
- Reducing typing errors for project/app/site names
- Discovering available projects without running `cwcli ls`
- Speeding up command execution

## Testing
- [x] All tests pass (27 tests, 92% coverage for completion_utils.py)
- [x] Code is formatted with Black
- [x] Linting passes with Ruff
- [x] Manual testing on Bash, Zsh, and Fish shells

## Documentation
- [x] README updated with tab completion section
- [x] CHANGELOG updated
- [x] Docstrings added to all completion functions
- [x] Testing guide created

## Related Issues
Closes #42
```

---

## Before Creating a PR

### Checklist

**Code Quality:**
- [ ] All tests pass: `uv run pytest --cov`
- [ ] Code formatted: `uv run black src/ tests/`
- [ ] Linting passes: `uv run ruff check src/ --fix`
- [ ] No type errors: `uv run mypy src/`

**Documentation:**
- [ ] User-facing changes described in the PR (CHANGELOG.md itself is written at release time, in the version-bump commit)
- [ ] README.md updated (if user-facing changes)
- [ ] Docstrings added/updated
- [ ] Code comments added where needed

**Git Hygiene:**
- [ ] Commits follow [commit message conventions](./commit-messages.md)
- [ ] Branch follows [naming conventions](./branch-naming.md)
- [ ] No merge conflicts with target branch
- [ ] Commits are logical and well-organized

**Testing:**
- [ ] New features have tests
- [ ] Bug fixes have regression tests
- [ ] Coverage maintained or improved

---

## Creating a PR

### Step 1: Push Your Branch

```bash
# Ensure branch is up to date with develop
git checkout develop
git pull origin develop
git checkout feat-my-feature
git merge develop  # Or rebase if you prefer

# Push to your fork
git push -u origin feat-my-feature
```

### Step 2: Open PR via GitHub UI

1. Go to https://github.com/karotkriss/caffeinated-whale-cli
2. Click "Pull requests" → "New pull request"
3. Select your branch
4. Fill out the PR template

### Step 3: Fill Out Details

**Title:**
```
feat: add tab completion support
```

**Base branch:** `develop` (usually)

**Description:** Use the template above

**Labels:** Add appropriate labels:
- `enhancement` for features
- `bug` for fixes
- `documentation` for docs
- `testing` for test additions

---

## PR Workflow

### After Creating PR

1. **CI checks run automatically:**
   - Lint workflow (Black, Ruff)
   - Test workflow (the fast `unit` pytest tier, plus a zero-error mypy gate)
   - E2E workflow (real-Docker `e2e` tier, v14/v15/v16 matrix) - required on PRs into `develop`/`master`, or on-demand via the `e2e` label

2. **Address CI failures:**
   ```bash
   # Fix issues locally
   uv run black src/ tests/
   uv run ruff check src/ --fix

   # Commit and push
   git add .
   git commit -m "style: fix linting issues"
   git push
   ```

3. **Respond to review comments:**
   - Make requested changes
   - Push new commits (don't force push during review)
   - Reply to comments when done

### During Review

**For Contributors:**
- Be responsive to feedback
- Don't force push after review starts
- Mark conversations as resolved when addressed
- Ask questions if feedback is unclear

**For Reviewers:**
- Be constructive and specific
- Suggest code examples when helpful
- Approve when ready or request changes
- Use "Comment" for non-blocking suggestions

---

## Merging

### When Ready to Merge

**Requirements:**
- ✅ All CI checks pass
- ✅ At least one approval (project-specific)
- ✅ No merge conflicts
- ✅ All review comments addressed

### Merge Strategy

**Squash and merge (recommended):**
- Combines all commits into one
- Uses PR title as commit message
- Keeps develop branch history clean

```
feat: add tab completion support (#42)
```

**Merge commit (alternative):**
- Preserves all individual commits
- Useful for large features with meaningful commit history

**Rebase and merge (rare):**
- Replays commits on top of base branch
- No merge commit created
- Use when commits are already well-organized

### After Merge

1. **Delete feature branch:**
   ```bash
   # Via GitHub UI or locally
   git branch -d feat-my-feature
   git push origin --delete feat-my-feature
   ```

2. **Update local develop:**
   ```bash
   git checkout develop
   git pull origin develop
   ```

3. **Close related issues** (if not auto-closed)

---

## PR Size Guidelines

### Keep PRs Manageable

**Good PR sizes:**
- **Small:** 1-100 lines changed (ideal)
- **Medium:** 100-500 lines changed (acceptable)
- **Large:** 500-1000 lines changed (needs justification)
- **Too large:** 1000+ lines (break it up!)

### When to Split PRs

If your PR includes:
- Multiple unrelated features → Split into separate PRs
- Feature + refactor → PR for refactor first, then feature
- Feature + documentation → Can combine if related
- Feature + tests → Always combine (tests are part of feature)

**Example split:**
```
Before: PR with 2000 lines (port utils + tab completion)

After:
1. PR #1: refactor: extract port utilities module
2. PR #2: feat: add tab completion support
```

---

## Draft PRs

Use draft PRs for work-in-progress:

**When to use:**
- Getting early feedback on approach
- Running CI checks on incomplete work
- Showing progress on large features

**How to create:**
```
GitHub UI: Create PR → Select "Create draft pull request"
```

**Draft PR title:**
```
feat: add tab completion support
```

Don't use "WIP:" prefix - GitHub marks it as draft automatically.

**When ready:**
```
GitHub UI: "Ready for review" button
```

---

## Common Scenarios

### Scenario 1: PR Has Merge Conflicts

```bash
# Update your branch with latest develop
git checkout feat-my-feature
git fetch origin
git merge origin/develop

# Resolve conflicts
# Edit conflicted files
git add .
git commit -m "chore: merge develop and resolve conflicts"
git push
```

### Scenario 2: Need to Update After Review

```bash
# Make changes
# ... edit files ...

# Commit with descriptive message
git add .
git commit -m "fix: address review feedback on error handling"
git push
```

### Scenario 3: CI Failing on Your PR

```bash
# Check what failed in GitHub Actions tab
# Fix issues locally

# Format and lint
uv run black src/ tests/
uv run ruff check src/ --fix

# Run tests
uv run pytest --cov

# Commit and push
git add .
git commit -m "style: fix linting issues"
git push
```

### Scenario 4: PR Too Large

```bash
# Create smaller PRs from parts of your work

# Option 1: Cherry-pick specific commits
git checkout -b feat-part-1
git cherry-pick <commit-hash-1> <commit-hash-2>
git push -u origin feat-part-1

# Option 2: Start fresh with subset of changes
git checkout develop
git checkout -b feat-part-1
# Copy specific files or changes
git add file1.py file2.py
git commit -m "feat: add part 1 of feature"
```

---

## PR Title Examples by Type

### Features

```
feat: add tab completion for all commands
feat: implement port conflict detection
feat: add VS Code integration to open command
feat: support multiple app updates in single command
```

### Bug Fixes

```
fix: ctrl + c loop in start command
fix: port conflict detection in mixed scenarios
fix: Windows compatibility for VS Code extensions
fix: spinner freezing during blocking operations
```

### Documentation

```
docs: add pull request guidelines
docs: restructure documentation into categories
docs: add testing guide with examples
docs: improve README contributing section
```

### Refactoring

```
refactor: consolidate CI/CD documentation
refactor: extract port utilities into separate module
refactor: simplify container startup logic
refactor: standardize imports across commands
```

### Chores

```
chore: bump version to 0.10.0
chore: update dependencies
chore: merge develop and resolve conflicts
chore: prepare for v0.10.0 release
```

### Tests

```
test: add tab completion utilities test suite
test: add port conflict detection tests
test: increase coverage for docker utilities
test: add integration tests for commands
```

---

## Review Process

### For Contributors

**What reviewers look for:**
1. **Code quality** - Readable, maintainable, follows project style
2. **Tests** - Adequate coverage, edge cases handled
3. **Documentation** - Clear docstrings, README updated
4. **Commits** - Well-organized, follow conventions
5. **No regressions** - Doesn't break existing functionality

**How to get faster reviews:**
- Keep PRs small and focused
- Write clear descriptions
- Add tests and documentation
- Respond promptly to feedback
- Run CI checks locally first

### For Reviewers

**Review checklist:**
- [ ] PR title follows conventions
- [ ] Changes match description
- [ ] Code is readable and maintainable
- [ ] Tests cover new functionality
- [ ] Documentation is updated
- [ ] No security issues
- [ ] No performance regressions
- [ ] CHANGELOG updated

**Review etiquette:**
- Be constructive and kind
- Explain reasoning for requested changes
- Praise good code
- Ask questions rather than make demands
- Approve when ready

---

## Resources

### Related Documentation

- [Git Workflow](./git-workflow.md) - Complete workflow guide
- [Commit Messages](./commit-messages.md) - How to write commits
- [Branch Naming](./branch-naming.md) - Branch naming conventions
- [Code Quality](./code-quality.md) - Formatting and linting standards

### External Resources

- [GitHub PR Documentation](https://docs.github.com/en/pull-requests)
- [How to Write a Good PR](https://github.blog/2015-01-21-how-to-write-the-perfect-pull-request/)
- [Code Review Best Practices](https://google.github.io/eng-practices/review/)

---

## Quick Reference

**Create PR:**
```bash
git push -u origin feat-my-feature
# Then open PR via GitHub UI
```

**PR title format:**
```
<type>: <description>
```

**Before submitting:**
```bash
uv run black src/ tests/
uv run ruff check src/ --fix
uv run pytest --cov
```

**After review:**
```bash
# Make changes
git add .
git commit -m "fix: address review feedback"
git push
```

**After merge:**
```bash
git checkout develop
git pull origin develop
git branch -d feat-my-feature
```

---

**Remember:** Good PRs are small, well-documented, and easy to review. When in doubt, ask questions!
