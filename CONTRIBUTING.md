# Contributing to Caffeinated Whale CLI

Thank you for your interest in contributing to Caffeinated Whale CLI! This document provides a quick start guide for contributors.

## Quick Start

1. **Fork the repository** on GitHub
2. **Clone your fork:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/caffeinated-whale-cli.git
   cd caffeinated-whale-cli
   ```
3. **Install dependencies:**
   ```bash
   uv sync --all-extras
   ```
4. **Create a feature branch:**
   ```bash
   git checkout -b feat-my-feature
   ```
5. **Make your changes** and commit using [conventional commits](#commit-messages)
6. **Run tests:**
   ```bash
   uv run pytest --cov
   ```
7. **Push and create a Pull Request**

## Documentation

Complete contributing documentation is available in the [`docs/contributing/`](./docs/contributing/) directory:

- **[Git Workflow Guide](./docs/contributing/git-workflow.md)** - Complete workflow, PR process, release workflow
- **[Commit Message Guide](./docs/contributing/commit-messages.md)** - How to write conventional commits
- **[Branch Naming Guide](./docs/contributing/branch-naming.md)** - Branch naming standards
- **[Code Quality Guide](./docs/contributing/code-quality.md)** - Formatting with Black, linting with Ruff
- **[Chores Guide](./docs/contributing/chores.md)** - Maintenance task management

## Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/) for all commit messages:

```
<type>: <subject>

[optional body]

[optional footer]
```

### Types

- `feat:` - New features
- `fix:` - Bug fixes
- `chore:` - Maintenance (version bumps, dependencies)
- `docs:` - Documentation changes
- `test:` - Test additions or updates
- `refactor:` - Code restructuring without behavior changes
- `perf:` - Performance improvements
- `style:` - Code formatting
- `build:` - Build system changes
- `ci:` - CI/CD changes

### Examples

```bash
git commit -m "feat: add tab completion for project names"
git commit -m "fix: ctrl + c loop in start command"
git commit -m "chore: bump version to 0.9.2"
git commit -m "docs: add testing guide"
git commit -m "test: add tests for port utilities"
```

See [Commit Message Guide](./docs/contributing/commit-messages.md) for detailed documentation.

## Branch Naming

Branch names should follow this pattern:

```
<type>-<description>
```

### Examples

```bash
git checkout -b feat-tab-completion
git checkout -b fix-port-conflicts
git checkout -b chore-update-deps
git checkout -b docs-api-reference
```

See [Branch Naming Guide](./docs/contributing/branch-naming.md) for detailed documentation.

## Development Workflow

### 1. Setup Development Environment

```bash
# Install dependencies
uv sync --all-extras

# Verify installation
uv run cwcli --version
```

### 2. Make Changes

```bash
# Create feature branch
git checkout -b feat-my-feature

# Write code
# ... make changes ...

# Format code
uv run black src/ tests/

# Lint code
uv run ruff check src/ --fix
```

### 3. Write Tests

```bash
# Create test file
touch tests/test_my_module.py

# Write tests (see docs/testing/guide.md)

# Run tests
uv run pytest tests/test_my_module.py

# Check coverage
uv run pytest --cov=caffeinated_whale_cli.my_module
```

See [Testing Guide](./docs/testing/guide.md) for complete testing documentation.

### 4. Commit Changes

```bash
# Stage changes
git add .

# Commit with conventional format
git commit -m "feat: add my feature

Detailed description of what the feature does.

- Key point 1
- Key point 2"
```

### 5. Push and Create Pull Request

```bash
# Push to your fork
git push -u origin feat-my-feature

# Create Pull Request on GitHub
# Fill out the PR template
```

## Pull Request Guidelines

### Before Submitting

- [ ] All tests pass (`uv run pytest`)
- [ ] Code is formatted (`uv run black src/ tests/`)
- [ ] Linting passes (`uv run ruff check src/`)
- [ ] Documentation is updated
- [ ] Commit messages follow conventions
- [ ] Branch name follows conventions
- [ ] Tests are written for new features

### PR Title Format

```
<type>: <description>
```

Examples:
```
feat: add tab completion
fix: port conflict detection
docs: improve testing guide
```

### PR Description Template

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
- [ ] Coverage maintained/improved

## Documentation
- [ ] README updated (if needed)
- [ ] Code comments added
- [ ] Docstrings updated

## Related Issues
Closes #XX
Fixes #YY
See also #ZZ
```

## Code Style

### Python

- **Formatter**: Black (line length 100)
- **Linter**: Ruff
- **Type Hints**: Use where applicable
- **Docstrings**: Use for all public functions/classes

```python
def my_function(param: str) -> bool:
    """
    Brief description of function.

    Args:
        param: Description of parameter

    Returns:
        Description of return value
    """
    pass
```

### Running Code Quality Checks

```bash
# Format code
uv run black src/ tests/

# Lint code
uv run ruff check src/ --fix

# Type check (zero-error gate in CI; keep this clean)
uv run mypy src/
```

## Testing

### Writing Tests

```python
"""
Tests for my_module.

Description of what's being tested.
"""

import pytest
from unittest.mock import Mock, patch

from caffeinated_whale_cli.my_module import my_function


class TestMyFunction:
    """Tests for my_function."""

    def test_happy_path(self):
        """Should handle normal case correctly."""
        result = my_function("input")
        assert result == expected

    def test_error_handling(self):
        """Should handle errors gracefully."""
        result = my_function(invalid_input)
        assert result is None
```

### Running Tests

```bash
# All tests
uv run pytest

# Specific test file
uv run pytest tests/test_my_module.py

# With coverage
uv run pytest --cov

# Verbose output
uv run pytest -v
```

See [Testing Guide](./docs/testing/guide.md) for comprehensive testing documentation.

## Documentation

### Where to Add Documentation

- **Code changes**: Update docstrings
- **New features**: Update README.md and relevant docs
- **Contributing guidelines**: Update docs/contributing/
- **Testing guides**: Update docs/testing/
- **Technical docs**: Update docs/technical/

### Documentation Style

- Use clear, concise language
- Include practical examples
- Add code snippets where helpful
- Link to related documentation
- Keep table of contents updated

## Getting Help

- **Questions**: Open a [GitHub Issue](https://github.com/karotkriss/caffeinated-whale-cli/issues)
- **Documentation**: Check the [docs folder](./docs/)
- **Examples**: See existing code in `src/caffeinated_whale_cli/`
- **Tests**: See test examples in `tests/`

## Code of Conduct

Be respectful and considerate in all interactions. We're all here to build something great together.

## License

By contributing to Caffeinated Whale CLI, you agree that your contributions will be licensed under the MIT License.

---

**Thank you for contributing!** Your help makes this project better for everyone.
