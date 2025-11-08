# Code Quality and Linting

This project uses automated code quality checks in CI/CD to maintain consistent code style and catch common issues.

## Tools

### 1. Black (Code Formatter)

**Purpose:** Enforce consistent code formatting

**Configuration:** `pyproject.toml`
```toml
[tool.black]
line-length = 100
target-version = ["py313"]
skip-string-normalization = false
```

**Run locally:**
```bash
# Check formatting
uv run black --check src/

# Auto-fix formatting
uv run black src/
```

---

### 2. Ruff (Linter)

**Purpose:** Fast Python linter (replaces flake8, isort, pyupgrade, etc.)

**Configuration:** `pyproject.toml`
```toml
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort (import sorting)
    "N",   # pep8-naming
    "UP",  # pyupgrade
    "B",   # flake8-bugbear
]
ignore = [
    "E501",  # line too long (handled by black)
]
```

**Run locally:**
```bash
# Check for issues
uv run ruff check src/

# Auto-fix issues
uv run ruff check src/ --fix

# Show what would be fixed (dry run)
uv run ruff check src/ --fix --diff
```

---

### 3. mypy (Type Checker)

**Purpose:** Static type checking for Python

**Configuration:** `pyproject.toml`
```toml
[tool.mypy]
python_version = "3.10"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = false
ignore_missing_imports = true
```

**Run locally:**
```bash
# Type check code
uv run mypy src/ --install-types --non-interactive
```

---

## CI/CD Integration

### Lint Workflow

**File:** `.github/workflows/lint.yml`

**Triggers:**
- Push to any branch
- Pull requests to any branch

**Steps:**
1. ✅ Check Black formatting (`--check` mode, no auto-fix)
2. ✅ Run Ruff linting (reports all errors)
3. ✅ Run mypy type checking

**Behavior:**
- **Fails if ANY check fails** (no `continue-on-error`)
- Blocks PRs until all checks pass
- Fast feedback (~30-60 seconds)

---

## Pre-commit Workflow

To run all checks before committing:

```bash
# Install dev dependencies
uv sync --all-extras

# Run all checks
uv run black --check src/ && \
  uv run ruff check src/ && \
  uv run mypy src/
```

**If checks fail:**
```bash
# Auto-fix what's possible
uv run black src/
uv run ruff check src/ --fix

# Review changes
git diff

# Commit fixes
git add .
git commit -m "style: fix linting issues"
```

---

## Current Status

As of the latest check, the codebase has:

- ✅ **Black:** All files properly formatted
- ⚠️ **Ruff:** 126 issues found (92 auto-fixable)
  - Import sorting issues
  - Unused imports
  - Code style violations
- ℹ️ **mypy:** Type checking enabled but permissive

---

## Fixing Ruff Issues

### Auto-fix most issues:

```bash
uv run ruff check src/ --fix
```

This will automatically:
- Sort imports alphabetically
- Remove unused imports
- Fix obvious code style issues
- Apply pyupgrade suggestions

### Review changes:

```bash
git diff src/
```

### Common Ruff issues:

**I001 - Unsorted imports:**
```python
# Before
import typer
from rich.console import Console
import docker

# After (fixed by ruff --fix)
import docker
import typer
from rich.console import Console
```

**F401 - Unused import:**
```python
# Before
import sys  # Not used anywhere

# After (fixed by ruff --fix)
# (import removed)
```

**UP - Outdated Python syntax:**
```python
# Before
from typing import List

def get_names() -> List[str]:
    pass

# After (fixed by ruff --fix)
def get_names() -> list[str]:  # Modern Python 3.10+ syntax
    pass
```

---

## Disabling Checks

### Per-file:
```python
# ruff: noqa
```

### Per-line:
```python
import something  # noqa: F401
```

### Per-rule:
```python
# ruff: noqa: F401
import something
```

### In pyproject.toml:
```toml
[tool.ruff.lint]
ignore = [
    "E501",  # line too long
    "F401",  # unused import
]
```

---

## Development Dependencies

All linting tools are in the `dev` optional dependency group:

```toml
[project.optional-dependencies]
dev = [
    "ruff>=0.8.0",
    "mypy>=1.13.0",
    "pytest>=8.0.0",
    "pytest-cov>=6.0.0",
]
```

**Install:**
```bash
uv sync --all-extras
```

---

## Recommended Workflow

### 1. Before Committing:
```bash
# Auto-fix what you can
uv run black src/
uv run ruff check src/ --fix

# Check remaining issues
uv run ruff check src/
uv run mypy src/
```

### 2. Before Pushing:
```bash
# Ensure all checks pass
uv run black --check src/ && \
  uv run ruff check src/ && \
  uv run mypy src/
```

### 3. In PR:
- Lint workflow runs automatically
- Fix any failures before merging
- Use `ruff check src/ --fix` for quick fixes

---

## Future Improvements

- [ ] Add pre-commit hooks for automatic local checking
- [ ] Increase mypy strictness (`disallow_untyped_defs = true`)
- [ ] Add docstring linting (pydocstyle)
- [ ] Add security checks (bandit)
- [ ] Add complexity checks (mccabe)

---

## Resources

- [Black Documentation](https://black.readthedocs.io/)
- [Ruff Documentation](https://docs.astral.sh/ruff/)
- [mypy Documentation](https://mypy.readthedocs.io/)
- [PEP 8 Style Guide](https://pep8.org/)
