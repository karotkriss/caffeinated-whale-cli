# Documentation

This directory contains comprehensive documentation for the caffeinated-whale-cli project.

## Quick Start

New to the project? Start here:

1. **[Git Workflow Guide](./git-workflow-guide.md)** - Complete workflow overview
2. **[Testing Guide](./testing-guide.md)** - How to write and run tests
3. **Main README** - [../README.md](../README.md) - Project overview and CLI commands

## Git & Development Workflow

### Core Guides

| Guide | Purpose | Key Topics |
|-------|---------|------------|
| **[Git Workflow Guide](./git-workflow-guide.md)** | Master workflow guide | Complete workflow, PR process, release workflow |
| **[Commit Message Guide](./commit-message-guide.md)** | Writing commit messages | Conventional commits, types, examples, analysis |
| **[Branch Naming Guide](./branch-naming-guide.md)** | Naming branches | Patterns, types, lifecycle, best practices |
| **[Chores Guide](./chores-guide.md)** | Managing maintenance tasks | Version bumps, CHANGELOG, dependencies, automation |

### Quick References

**Commit Format:**
```
<type>: <subject>

[optional body]

[optional footer]
```

**Branch Format:**
```
<type>-<description>
```

**Types:** `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`, `style`, `build`, `ci`

## Testing

| Guide | Purpose | Key Topics |
|-------|---------|------------|
| **[Testing Guide](./testing-guide.md)** | Complete testing guide | Writing tests, running tests, coverage, best practices |
| **[Test Coverage Summary](./test-coverage-summary.md)** | Current test status | Coverage metrics, what's tested, priorities |
| **[Test Implementation Checklist](./test-implementation-checklist.md)** | Implementation tracking | Tasks completed, verification steps |

**Quick Commands:**
```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov

# Run specific test file
uv run pytest tests/test_completion_utils.py
```

## Technical Documentation

| Document | Purpose |
|----------|---------|
| **[bench.md](./bench.md)** | Bench management documentation |

## Project Analysis

### Commit Statistics (79 commits analyzed)

| Type | Count | % | Most Common Use |
|------|-------|---|-----------------|
| `chore:` | 29 | 36.7% | Version bumps, CHANGELOG updates |
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

### Current Test Coverage

- **completion_utils.py**: 92% (27 tests)
- **Overall project**: 7.46% (only completion utils tested)
- **Target**: Expand coverage to port utilities, Docker utils, commands

## Documentation Structure

```
docs/
├── README.md                           # This file
├── git-workflow-guide.md               # Master workflow guide
├── commit-message-guide.md             # Commit message conventions
├── branch-naming-guide.md              # Branch naming conventions
├── chores-guide.md                     # Maintenance task management
├── testing-guide.md                    # Complete testing guide
├── test-coverage-summary.md            # Test coverage status
├── test-implementation-checklist.md    # Implementation tracking
└── bench.md                            # Bench management docs
```

## Common Tasks

### Starting New Work

```bash
# Create feature branch
git checkout develop
git pull origin develop
git checkout -b feat-my-feature

# Make changes
git commit -m "feat: add my feature"

# Create PR
git push -u origin feat-my-feature
```

### Writing Tests

```bash
# Create test file
touch tests/test_my_module.py

# Write tests
# See testing-guide.md for examples

# Run tests
uv run pytest tests/test_my_module.py

# Check coverage
uv run pytest --cov=my_module
```

### Preparing Release

```bash
# Update version, CHANGELOG, README
git commit -m "chore: release v0.9.2

- Bump version to 0.9.2
- Update CHANGELOG with new features
- Update README with usage examples"

# Tag and push
git tag v0.9.2
git push origin develop --tags
```

### Running Quality Checks

```bash
# Format code
uv run black src/ tests/

# Lint code
uv run ruff check src/ --fix

# Run tests
uv run pytest --cov
```

## Decision Trees

### Commit Type Selection

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

### Branch Type Selection

```
New feature?              → feat-<description>
Bug fix?                  → fix-<description>
Maintenance/tooling?      → chore-<description>
Documentation?            → docs-<description>
Code restructure?         → refactor-<description>
Adding tests?             → test-<description>
Release preparation?      → release-v<version>
Critical production fix?  → hotfix-<description>
```

## Best Practices Summary

### Commits
- ✅ Use conventional commit format
- ✅ One logical change per commit
- ✅ Write descriptive messages
- ✅ Use imperative mood
- ✅ Include context in body
- ✅ Reference issues in footer

### Branches
- ✅ Use type prefixes (feat-, fix-, etc.)
- ✅ Keep names short and descriptive
- ✅ Use kebab-case
- ✅ Delete after merge
- ✅ Stay up to date with develop

### Testing
- ✅ Write tests for new features
- ✅ Maintain 80%+ coverage
- ✅ Test happy path and edge cases
- ✅ Use descriptive test names
- ✅ Mock external dependencies

### Code Quality
- ✅ Format with Black
- ✅ Lint with Ruff
- ✅ Type hints where applicable
- ✅ Clear function/class names
- ✅ Comprehensive docstrings

## Contributing

1. **Fork the repository**
2. **Create a feature branch** - Follow [Branch Naming Guide](./branch-naming-guide.md)
3. **Make your changes** - Follow code quality standards
4. **Write tests** - See [Testing Guide](./testing-guide.md)
5. **Commit changes** - Follow [Commit Message Guide](./commit-message-guide.md)
6. **Push to branch** - `git push origin feat-my-feature`
7. **Create Pull Request** - Follow PR template

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

## Getting Help

- **Issues**: Report bugs or request features on [GitHub Issues](https://github.com/karotkriss/caffeinated-whale-cli/issues)
- **Documentation**: Check this docs folder
- **Examples**: See test files in `tests/` directory
- **Code**: Browse source in `src/caffeinated_whale_cli/`

## Recent Updates

### Latest Documentation Added
- ✅ Git Workflow Guide - Complete workflow documentation
- ✅ Commit Message Guide - Conventional commit standards
- ✅ Branch Naming Guide - Branch naming conventions
- ✅ Chores Guide - Maintenance task management
- ✅ Testing Guide - Comprehensive testing documentation
- ✅ Test Coverage Summary - Current coverage status

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
- 📝 CI/CD pipeline
