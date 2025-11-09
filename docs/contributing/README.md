# Contributing to Caffeinated Whale CLI

Welcome! This directory contains all documentation related to contributing to the project.

## Quick Start

1. **[Git Workflow Guide](./git-workflow.md)** - Start here for the complete workflow
2. Fork the repository
3. Create a feature branch following [Branch Naming](./branch-naming.md)
4. Make your changes
5. Write tests (see [../testing/](../testing/))
6. Commit using [Commit Message Guide](./commit-messages.md)
7. Push and create a Pull Request

## Documentation

### Essential Guides

| Guide | Purpose | Start Here If... |
|-------|---------|------------------|
| **[Git Workflow](./git-workflow.md)** | Complete workflow overview | You're new to contributing |
| **[Commit Messages](./commit-messages.md)** | How to write commits | You need commit message help |
| **[Branch Naming](./branch-naming.md)** | How to name branches | You're creating a new branch |
| **[Pull Requests](./pull-requests.md)** | How to create and manage PRs | You're creating a pull request |
| **[Code Quality](./code-quality.md)** | Formatting, linting, standards | You're writing code |
| **[Chores](./chores.md)** | Maintenance tasks | You're doing version bumps or maintenance |
| **[CI/CD](./ci-cd.md)** | GitHub Actions workflows | You're setting up CI or releasing |

### Workflow Overview

```text
develop
  ├── feat-your-feature       Create branch with type prefix
  │   ├── feat: add feature   Commit with conventional format
  │   ├── test: add tests     Add tests for your changes
  │   └── docs: update docs   Update documentation
  └── Pull Request            Merge back to develop
```

## Commit Message Format

```text
<type>: <subject>

[optional body]

[optional footer]
```

**Types:** `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`, `style`, `build`, `ci`

**Examples:**
```text
feat: add tab completion
fix: ctrl + c loop in start command
chore: bump version to 0.9.2
docs: add API reference
test: add port utility tests
```

See [Commit Messages Guide](./commit-messages.md) for detailed documentation.

## Branch Naming Format

```text
<type>-<description>
```

**Examples:**
```text
feat-tab-completion
fix-port-conflicts
chore-update-deps
docs-api-reference
```

See [Branch Naming Guide](./branch-naming.md) for detailed documentation.

## Development Workflow

### 1. Setup
```bash
# Clone repository
git clone https://github.com/karotkriss/caffeinated-whale-cli.git
cd caffeinated-whale-cli

# Install dependencies
uv sync --all-extras

# Run tests
uv run pytest
```

### 2. Create Branch
```bash
git checkout develop
git pull origin develop
git checkout -b feat-your-feature
```

### 3. Make Changes
```bash
# Write code
# Write tests (see ../testing/guide.md)
# Update docs

# Commit changes
git add .
git commit -m "feat: add your feature"
```

### 4. Before Push
```bash
# Format code
uv run black src/ tests/

# Lint code
uv run ruff check src/ --fix

# Run tests
uv run pytest --cov
```

### 5. Create Pull Request
```bash
git push -u origin feat-your-feature
# Then create PR via GitHub UI
```

## Code Review Process

### For Contributors

Before requesting review:
- [ ] All tests pass locally
- [ ] Code is formatted with Black
- [ ] Linting passes with Ruff
- [ ] Documentation is updated
- [ ] Commit messages follow conventions
- [ ] Branch name follows conventions

### For Reviewers

Checklist:
- [ ] Code quality and readability
- [ ] Tests are comprehensive
- [ ] Documentation is clear
- [ ] Commit messages are descriptive
- [ ] No security issues
- [ ] Performance considerations

## Release Process

See [Chores Guide](./chores.md) for details on release workflow.

**Quick summary:**
```bash
# 1. Update version, CHANGELOG, README
git commit -m "chore: release v0.9.2

- Bump version to 0.9.2
- Update CHANGELOG with new features
- Update README"

# 2. Tag release
git tag v0.9.2

# 3. Push
git push origin develop --tags
```

## Common Tasks

### Adding a New Feature
```bash
# 1. Create branch
git checkout -b feat-my-feature

# 2. Implement feature
# ... write code ...
git commit -m "feat: add my feature"

# 3. Add tests
# ... write tests ...
git commit -m "test: add feature tests"

# 4. Update docs
# ... update README/docs ...
git commit -m "docs: document new feature"

# 5. Push and create PR
git push -u origin feat-my-feature
```

### Fixing a Bug
```bash
# 1. Create branch
git checkout -b fix-bug-description

# 2. Fix bug
git commit -m "fix: description of bug fix"

# 3. Add regression test
git commit -m "test: add regression test for bug"

# 4. Push and create PR
git push -u origin fix-bug-description
```

### Updating Dependencies
```bash
# 1. Update dependencies
uv sync --upgrade

# 2. Test changes
uv run pytest

# 3. Commit
git add uv.lock pyproject.toml
git commit -m "chore: update dependencies"
```

## Tools & Automation

### Pre-commit Hooks

See [Git Workflow Guide](./git-workflow.md#git-hooks) for setup.

### Commitlint

Enforce commit message format:
```bash
npm install --save-dev @commitlint/{cli,config-conventional}
```

### Release Automation

Use release-it for automated releases:
```bash
npm install --save-dev release-it
npx release-it
```

## Questions?

- **Issues:** Open an issue on [GitHub](https://github.com/karotkriss/caffeinated-whale-cli/issues)
- **Discussions:** Start a discussion on GitHub
- **Documentation:** Check the other guides in this directory

## Related Documentation

- [Testing Guide](../testing/guide.md) - How to write and run tests
- [Technical Documentation](../technical/) - Technical deep-dives
- [Main README](../../README.md) - Project overview

## Thank You!

Thank you for contributing to Caffeinated Whale CLI! Your contributions help make this tool better for everyone.
