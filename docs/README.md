# Documentation

Welcome to the caffeinated-whale-cli documentation!

## Quick Start

New to the project? Start here:

1. **[Contributing Guide](./contributing/)** - How to contribute
2. **[Testing Guide](./testing/)** - How to write and run tests
3. **[Main README](../README.md)** - Project overview and CLI commands

## Documentation Structure

```
docs/
├── contributing/           # Git workflow & contribution guidelines
│   ├── git-workflow.md    # Complete workflow guide
│   ├── commit-messages.md # Commit message conventions
│   ├── branch-naming.md   # Branch naming standards
│   ├── pull-requests.md   # Pull request guidelines
│   ├── code-quality.md    # Formatting, linting, standards
│   ├── chores.md          # Maintenance task management
│   └── ci-cd.md           # GitHub Actions & releases
│
├── testing/               # Testing documentation
│   ├── guide.md          # Complete testing guide
│   ├── coverage-summary.md       # Current coverage status
│   └── implementation-checklist.md   # Test tracking
│
└── technical/             # Technical documentation & API reference
    └── bench.md           # Bench management deep-dive
```

## Contributing

Everything you need to contribute to the project.

### Essential Guides

| Guide | Purpose | Start Here If... |
|-------|---------|------------------|
| **[Git Workflow](./contributing/git-workflow.md)** | Complete contribution workflow | You're new to contributing |
| **[Commit Messages](./contributing/commit-messages.md)** | How to write commit messages | You need commit help |
| **[Branch Naming](./contributing/branch-naming.md)** | How to name branches | You're creating a branch |
| **[Pull Requests](./contributing/pull-requests.md)** | How to create and manage PRs | You're creating a pull request |
| **[Code Quality](./contributing/code-quality.md)** | Formatting, linting, standards | You're writing code |
| **[Chores](./contributing/chores.md)** | Maintenance tasks | You're doing version bumps |
| **[CI/CD](./contributing/ci-cd.md)** | GitHub Actions workflows | You're setting up CI or releasing |

### Quick Reference

**Commit Format:**
```
<type>: <subject>
```

**Types:** `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`, `style`, `build`, `ci`

**Branch Format:**
```
<type>-<description>
```

**Examples:**
```bash
# Commits
git commit -m "feat: add tab completion"
git commit -m "fix: port conflict detection"
git commit -m "chore: bump version to 0.9.2"

# Branches
git checkout -b feat-tab-completion
git checkout -b fix-port-conflicts
git checkout -b chore-update-deps
```

See [Contributing Directory](./contributing/) for complete documentation.

## Testing

Everything related to writing and running tests.

### Essential Guides

| Guide | Purpose | When to Read |
|-------|---------|--------------|
| **[Testing Guide](./testing/guide.md)** | How to write tests | When writing new tests |
| **[Coverage Summary](./testing/coverage-summary.md)** | Current test status | To see what's tested |
| **[Implementation Checklist](./testing/implementation-checklist.md)** | Test tracking | To see progress |

### Quick Reference

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov

# Run specific test file
uv run pytest tests/test_completion_utils.py

# Generate HTML coverage report
uv run pytest --cov --cov-report=html
```

### Current Status

- **completion_utils.py**: 92% coverage (27 tests) ✅
- **Overall project**: 7.46% coverage
- **Target**: Expand to port utilities, Docker utils, commands

See [Testing Directory](./testing/) for complete documentation.

## Technical Documentation

Deep-dives into architecture, API, and implementation details.

### Available Documentation

| Document | Topics Covered |
|----------|----------------|
| **[Bench Management](./technical/bench.md)** | Bench instances, sites, apps, cache system |

### Architecture Overview

```
caffeinated-whale-cli/
├── commands/          # CLI commands
├── utils/            # Utilities (Docker, caching, ports, etc.)
└── tests/            # Test suite
```

**Key Systems:**
- Docker container management
- Port conflict detection
- Project inspection & caching
- Tab completion
- VS Code integration

See [Technical Directory](./technical/) for complete documentation.

## Project Statistics

### Commit Analysis (79 commits)

| Type | Count | % | Most Common Use |
|------|-------|---|-----------------|
| `chore:` | 29 | 36.7% | Version bumps, CHANGELOG |
| `fix:` | 24 | 30.4% | Bug fixes |
| `feat:` | 16 | 20.3% | New features |
| Other | 10 | 12.6% | Merges, refactors |

### Active Branches

```
develop              # Main development branch
feat-tab-completion  # Tab completion feature
init                 # Initialization commands
ports                # Port conflict detection
```

### Test Coverage

- **Modules Tested**: 1 (completion_utils.py)
- **Test Files**: 1 (test_completion_utils.py)
- **Total Tests**: 27
- **Coverage**: 92% for tested module

## Common Tasks

### For Contributors

**Starting new work:**
```bash
git checkout develop
git pull origin develop
git checkout -b feat-my-feature
```

**Writing tests:**
```bash
# Create test file
touch tests/test_my_module.py

# Write tests (see testing/guide.md)

# Run tests
uv run pytest tests/test_my_module.py
```

**Before committing:**
```bash
# Format code
uv run black src/ tests/

# Lint code
uv run ruff check src/ --fix

# Run tests
uv run pytest --cov
```

### For Maintainers

**Preparing release:**
```bash
# Update version, CHANGELOG, README
git commit -m "chore: release v0.9.2

- Bump version to 0.9.2
- Update CHANGELOG
- Update README"

# Tag and push
git tag v0.9.2
git push origin develop --tags
```

## Decision Trees

### Choosing Commit Type

```
New feature?           → feat:
Bug fix?              → fix:
Version/maintenance?  → chore:
Documentation?        → docs:
Code restructure?     → refactor:
Adding tests?         → test:
Performance?          → perf:
```

### Choosing Branch Type

```
New feature?         → feat-<description>
Bug fix?            → fix-<description>
Maintenance?        → chore-<description>
Documentation?      → docs-<description>
```

See [Contributing Guides](./contributing/) for detailed decision trees.

## Best Practices Summary

### Commits
- ✅ Use conventional commit format
- ✅ One logical change per commit
- ✅ Write descriptive messages
- ✅ Use imperative mood ("add" not "added")

### Branches
- ✅ Use type prefixes (feat-, fix-, etc.)
- ✅ Keep names short and descriptive
- ✅ Use kebab-case
- ✅ Delete after merge

### Testing
- ✅ Write tests for new features
- ✅ Maintain 80%+ coverage
- ✅ Test happy path and edge cases
- ✅ Use descriptive test names

### Code Quality
- ✅ Format with Black
- ✅ Lint with Ruff
- ✅ Clear function/class names
- ✅ Comprehensive docstrings

## External Resources

### Project Links
- [GitHub Repository](https://github.com/karotkriss/caffeinated-whale-cli)
- [Issues](https://github.com/karotkriss/caffeinated-whale-cli/issues)
- [PyPI Package](https://pypi.org/project/caffeinated-whale-cli/)
- [Main README](../README.md)

### Standards & Conventions
- [Conventional Commits](https://www.conventionalcommits.org/)
- [Semantic Versioning](https://semver.org/)
- [Keep a Changelog](https://keepachangelog.com/)
- [PEP 8 Style Guide](https://pep8.org/)

### Tools & Frameworks
- [Typer](https://typer.tiangolo.com/) - CLI framework
- [Rich](https://rich.readthedocs.io/) - Terminal formatting
- [pytest](https://docs.pytest.org/) - Testing framework
- [Black](https://black.readthedocs.io/) - Code formatter
- [Ruff](https://docs.astral.sh/ruff/) - Linter
- [Docker SDK](https://docker-py.readthedocs.io/) - Docker Python SDK

## Getting Help

- **Issues**: Report bugs or request features on [GitHub Issues](https://github.com/karotkriss/caffeinated-whale-cli/issues)
- **Documentation**: Check the guides in this folder
- **Examples**: See test files in `tests/` directory
- **Code**: Browse source in `src/caffeinated_whale_cli/`

## Recent Documentation Updates

### Latest Additions
- ✅ Tab Completion Fixes (v0.10.1) - Fixed function signatures for Typer compatibility
- ✅ Pull Request Guidelines - Comprehensive PR naming and management guide
- ✅ Reorganized documentation into categories
- ✅ CI/CD Documentation - GitHub Actions workflows and automation
- ✅ Git Workflow Guide - Complete workflow documentation
- ✅ Commit Message Guide - Conventional commit standards
- ✅ Branch Naming Guide - Branch naming conventions
- ✅ Code Quality Guide - Black and Ruff standards
- ✅ Chores Guide - Maintenance task management
- ✅ Testing Guide - Comprehensive testing documentation
- ✅ Category READMEs - Quick navigation for each section

### Recent Features
- ✅ Tab completion (92% test coverage)
- ✅ Port conflict detection
- ✅ Cache system
- ✅ VS Code integration
- ✅ Update command with migrations

### Upcoming Priorities
- 📝 Expand test coverage to port utilities
- 📝 Add tests for Docker utilities
- 📝 Integration tests for commands
- 📝 Performance benchmarks
- 📝 API reference documentation

## Contributing to Documentation

Documentation improvements are always welcome!

**To update documentation:**

1. Find the relevant guide in the appropriate directory
2. Make your changes
3. Commit with `docs:` prefix:
   ```bash
   git commit -m "docs: improve testing guide examples"
   ```
4. Create a pull request

**Documentation standards:**
- Use clear, concise language
- Include practical examples
- Add links to related documentation
- Keep table of contents updated
- Test all code examples

---

**Thank you for using and contributing to Caffeinated Whale CLI!**
