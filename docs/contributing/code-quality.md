# Code Quality & Standards

This guide covers code quality standards, tools, and workflows for maintaining high-quality code in caffeinated-whale-cli.

## Overview

We use automated tools to ensure consistent code quality:

- **Black** - Code formatting
- **Ruff** - Fast Python linter
- **pytest** - Testing framework
- **mypy** - Type checking (zero-error gate in CI)

## Quick Reference

```bash
# Format code
uv run black src/ tests/

# Lint code
uv run ruff check src/

# Auto-fix lint issues
uv run ruff check src/ --fix

# Run tests
uv run pytest

# Run all checks
uv run black src/ tests/ && \
  uv run ruff check src/ --fix && \
  uv run pytest --cov
```

## Black - Code Formatter

### What is Black?

Black is an opinionated code formatter that automatically formats Python code to a consistent style.

**Philosophy:** "Any color you like, as long as it's black."

### Configuration

Located in `pyproject.toml`:

```toml
[tool.black]
line-length = 100
target-version = ["py313"]
skip-string-normalization = false
```

**Settings:**
- **Line length:** 100 characters (not the default 88)
- **Target version:** Python 3.13
- **String normalization:** Enabled (converts `'` to `"`)

### Usage

#### Check Formatting

```bash
# Check if files need formatting
uv run black --check src/

# Check specific file
uv run black --check src/caffeinated_whale_cli/commands/start.py

# Check both src and tests
uv run black --check src/ tests/
```

**Exit codes:**
- `0` - All files properly formatted
- `1` - Some files need formatting

#### Auto-Format

```bash
# Format all source files
uv run black src/

# Format tests too
uv run black src/ tests/

# Format specific file
uv run black src/caffeinated_whale_cli/commands/start.py
```

#### Preview Changes

```bash
# See what would change without modifying files
uv run black --diff src/
```

### What Black Changes

#### 1. Line Length

**Before:**
```python
def long_function_name(parameter_one, parameter_two, parameter_three, parameter_four, parameter_five):
    pass
```

**After:**
```python
def long_function_name(
    parameter_one,
    parameter_two,
    parameter_three,
    parameter_four,
    parameter_five,
):
    pass
```

#### 2. String Quotes

**Before:**
```python
message = 'Hello, world!'
```

**After:**
```python
message = "Hello, world!"
```

#### 3. Whitespace

**Before:**
```python
def function(a,b,c):
    result=a+b+c
    return result
```

**After:**
```python
def function(a, b, c):
    result = a + b + c
    return result
```

#### 4. Import Ordering

**Before:**
```python
import os, sys
from typing import List,Dict
```

**After:**
```python
import os
import sys
from typing import Dict, List
```

### Integration with Editors

#### VS Code

Install extension:
```json
{
  "python.formatting.provider": "black",
  "editor.formatOnSave": true,
  "python.formatting.blackArgs": ["--line-length", "100"]
}
```

#### PyCharm

1. Settings → Tools → Black
2. Enable "On save"
3. Set line length: 100

### When to Format

**Always format before:**
- Committing changes
- Creating pull requests
- Running CI/CD

**Quick pre-commit check:**
```bash
uv run black --check src/ tests/ && echo "✓ Formatting OK"
```

## Ruff - Fast Python Linter

### What is Ruff?

Ruff is an extremely fast Python linter written in Rust. It replaces Flake8, isort, pyupgrade, and more.

**Speed:** 10-100x faster than traditional linters

### Configuration

Located in `pyproject.toml`:

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

**What it checks:**
- Code errors and bugs
- Import ordering
- Naming conventions
- Python version compatibility
- Common anti-patterns

### Usage

#### Check for Issues

```bash
# Check all source files
uv run ruff check src/

# Check specific file
uv run ruff check src/caffeinated_whale_cli/commands/start.py

# Check with context
uv run ruff check src/ --show-source
```

#### Auto-Fix Issues

```bash
# Fix all auto-fixable issues
uv run ruff check src/ --fix

# Show what would be fixed (dry run)
uv run ruff check src/ --fix --diff

# Fix specific file
uv run ruff check src/caffeinated_whale_cli/commands/start.py --fix
```

#### Watch Mode

```bash
# Auto-fix on file changes
uv run ruff check src/ --fix --watch
```

### Common Ruff Rules

#### F401 - Unused Import

**Before:**
```python
import os  # Not used anywhere
import sys

def main():
    sys.exit(0)
```

**After (auto-fixed):**
```python
import sys

def main():
    sys.exit(0)
```

#### I001 - Unsorted Imports

**Before:**
```python
import typer
from rich.console import Console
import docker
```

**After (auto-fixed):**
```python
import docker
import typer
from rich.console import Console
```

#### F841 - Unused Variable

**Before:**
```python
def process():
    result = expensive_operation()  # Never used
    return True
```

**Issue:** Variable assigned but never used

**Fix:** Remove or use the variable

#### E712 - Comparison to True/False

**Before:**
```python
if value == True:
    pass
```

**After (auto-fixed):**
```python
if value:
    pass
```

#### UP - Python Upgrade Checks

**Before (Python 3.9 style):**
```python
from typing import List, Dict

def process() -> List[str]:
    items: Dict[str, int] = {}
```

**After (Python 3.10+ style):**
```python
def process() -> list[str]:
    items: dict[str, int] = {}
```

### Disabling Rules

#### Entire File

```python
# ruff: noqa
import something_unusual
```

#### Specific Line

```python
import something  # noqa: F401
```

#### Specific Rule

```python
# ruff: noqa: F401
import debug_tool  # Used in debugging
```

#### In pyproject.toml

```toml
[tool.ruff.lint]
ignore = [
    "E501",  # line too long
    "F401",  # unused import
]
```

## Type Checking with mypy

### What is mypy?

mypy is a static type checker for Python that verifies type hints.

**Status:** Runs as a zero-error gate in CI via the `Mypy` job in `.github/workflows/test.yml`. The historical ~50 errors across ~14 files were burned down to zero and `continue-on-error` was dropped from the step, so any new type error fails the job. Keep `uv run mypy src/` at zero errors: prefer accurate annotations over `# type: ignore` (there are none in `src/`), add the matching `types-*` stub package for an untyped third-party import rather than ignoring it, and do not loosen `[tool.mypy]` to make errors disappear. To make the check *required to merge*, a repo admin must also tick `Mypy` as a required status check in the `develop` branch-protection settings (same outstanding step as `Pytest`).

### Basic Usage

```bash
# Type check source code
uv run mypy src/

# Type check specific file
uv run mypy src/caffeinated_whale_cli/commands/start.py
```

### Type Hints Example

```python
from typing import Optional, List

def get_containers(project_name: str) -> List[Container]:
    """Get containers for a project."""
    pass

def find_project(name: str) -> Optional[Project]:
    """Find project by name, return None if not found."""
    pass
```

## Pre-Commit Workflow

### Manual Checks

**Before every commit:**

```bash
# 1. Format code
uv run black src/ tests/

# 2. Fix linting issues
uv run ruff check src/ --fix

# 3. Check for remaining issues
uv run ruff check src/

# 4. Run tests
uv run pytest
```

### Git Pre-Commit Hook

Create `.git/hooks/pre-commit`:

```bash
#!/bin/sh

echo "Running pre-commit checks..."

# Format check
echo "Checking formatting..."
uv run black --check src/ tests/
if [ $? -ne 0 ]; then
    echo "❌ Formatting failed. Run: uv run black src/ tests/"
    exit 1
fi

# Linting
echo "Running linter..."
uv run ruff check src/
if [ $? -ne 0 ]; then
    echo "❌ Linting failed. Run: uv run ruff check src/ --fix"
    exit 1
fi

# Tests
echo "Running tests..."
uv run pytest
if [ $? -ne 0 ]; then
    echo "❌ Tests failed."
    exit 1
fi

echo "✅ All checks passed!"
```

Make executable:
```bash
chmod +x .git/hooks/pre-commit
```

### Pre-Commit Framework

Install [pre-commit](https://pre-commit.com/):

```bash
pip install pre-commit
```

Create `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/psf/black
    rev: 24.1.0
    hooks:
      - id: black
        args: [--line-length=100]

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.1.9
    hooks:
      - id: ruff
        args: [--fix]

  - repo: local
    hooks:
      - id: pytest
        name: pytest
        entry: uv run pytest
        language: system
        pass_filenames: false
        always_run: true
```

Install hooks:
```bash
pre-commit install
```

## CI/CD Integration

### GitHub Actions Workflow

Located in `.github/workflows/lint.yml`:

```yaml
name: Lint

on:
  push:
    branches: ["**"]
  pull_request:
    branches: ["**"]

jobs:
  lint:
    name: Lint & Format Check
    runs-on: ubuntu-latest
    container:
      image: ghcr.io/astral-sh/uv:python3.12-bookworm

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Install dependencies
        run: uv sync --frozen --all-extras

      - name: Check code formatting with Black
        run: uv run black --check src/

      - name: Lint with Ruff
        run: uv run ruff check src/
```

**Triggers:**
- Every push to any branch
- Every pull request

**What it checks:**
- ✅ Black formatting
- ✅ Ruff linting

The test suite and type checking run in a separate `.github/workflows/test.yml` workflow (`Pytest` as the intended required gate, plus a zero-error `Mypy` gate). See the [CI/CD Workflows guide](./ci-cd.md) for details.

## Common Issues & Solutions

### Issue 1: Black and Ruff Conflict

**Problem:** Black formats code one way, Ruff complains about it.

**Solution:** This shouldn't happen with our config. Black is configured to ignore E501 (line length) in Ruff.

**Check config:**
```toml
[tool.ruff.lint]
ignore = ["E501"]  # Let Black handle line length
```

### Issue 2: Import Sorting Differences

**Problem:** Ruff and isort have different import ordering.

**Solution:** We use Ruff's import sorting (I rules), not isort.

**Disable isort:**
```bash
# Don't use isort
# Use Ruff instead: uv run ruff check --fix
```

### Issue 3: Too Many Ruff Errors

**Problem:** Running Ruff shows 100+ errors.

**Solution:** Fix incrementally:

```bash
# 1. Auto-fix what's possible
uv run ruff check src/ --fix

# 2. Review remaining issues
uv run ruff check src/

# 3. Fix manually or disable specific rules
```

### Issue 4: Black Changes Too Much

**Problem:** Black reformats entire files you didn't touch.

**Solution:** This is normal and good. Commit formatting separately:

```bash
# 1. Format everything first
uv run black src/
git add .
git commit -m "style: format code with black"

# 2. Then make your changes
# Your diffs will be clean
```

### Issue 5: CI Fails Locally Passes

**Problem:** Checks pass locally but fail in CI.

**Solution:** Ensure same tool versions:

```bash
# Check versions
uv run black --version
uv run ruff --version

# Update tools
uv sync --upgrade
```

## Code Style Guidelines

### Python Style

Follow [PEP 8](https://pep8.org/) as enforced by Black and Ruff.

#### Naming Conventions

```python
# Modules: lowercase with underscores
# File: docker_utils.py

# Classes: PascalCase
class DockerClient:
    pass

# Functions/Methods: snake_case
def get_containers():
    pass

# Constants: UPPER_SNAKE_CASE
MAX_RETRIES = 3

# Private: prefix with underscore
def _internal_helper():
    pass
```

#### Docstrings

Use Google-style docstrings:

```python
def complex_function(param1: str, param2: int = 0) -> bool:
    """
    Brief one-line description.

    More detailed explanation if needed.
    Can span multiple lines.

    Args:
        param1: Description of first parameter
        param2: Description of second parameter (default: 0)

    Returns:
        Description of return value

    Raises:
        ValueError: When param1 is empty
        RuntimeError: When operation fails

    Example:
        >>> complex_function("test", 5)
        True
    """
    pass
```

#### Import Ordering

Ruff automatically sorts imports:

```python
# 1. Standard library
import os
import sys
from typing import List, Optional

# 2. Third-party
import docker
import typer
from rich.console import Console

# 3. Local application
from ..utils.docker_utils import get_containers
from .utils import ensure_running
```

#### Line Length

**Maximum:** 100 characters

```python
# Bad - too long
def very_long_function_name(parameter_one, parameter_two, parameter_three, parameter_four, parameter_five, parameter_six):

# Good - wrapped
def very_long_function_name(
    parameter_one,
    parameter_two,
    parameter_three,
    parameter_four,
    parameter_five,
    parameter_six,
):
```

### Type Hints

Use type hints for function signatures:

```python
from typing import Optional, List, Dict

def process_data(
    items: List[str],
    config: Dict[str, Any],
    verbose: bool = False,
) -> Optional[str]:
    """Process data with configuration."""
    pass
```

**When to use:**
- ✅ Function parameters
- ✅ Function return types
- ✅ Class attributes
- ⚠️ Local variables (optional)

### Error Handling

```python
# Good - specific exceptions
try:
    result = risky_operation()
except ValueError as e:
    logger.error(f"Invalid value: {e}")
    raise
except ConnectionError as e:
    logger.error(f"Connection failed: {e}")
    return None

# Bad - bare except
try:
    result = risky_operation()
except:  # Too broad
    pass
```

## Performance Considerations

### Tool Speed Comparison

| Tool | Speed | Use Case |
|------|-------|----------|
| **Ruff** | ⚡⚡⚡ Very Fast | Linting (10-100x faster than Flake8) |
| **Black** | ⚡⚡ Fast | Formatting |
| **mypy** | ⚡ Moderate | Type checking (zero-error gate) |
| **pytest** | ⚡ Varies | Testing (depends on test count) |

### Optimization Tips

1. **Use Ruff instead of multiple tools**
   - Replaces: Flake8, isort, pyupgrade, etc.
   - Single fast tool vs. multiple slow tools

2. **Run Black before Ruff**
   - Black fixes formatting
   - Ruff checks remaining issues
   - Fewer Ruff errors to fix manually

3. **Cache Results**
   - Ruff caches by default
   - Black caches by default
   - Faster on subsequent runs

## Checklist

### Before Committing

- [ ] Run `uv run black src/ tests/`
- [ ] Run `uv run ruff check src/ --fix`
- [ ] Run `uv run pytest`
- [ ] Review changes with `git diff`
- [ ] Write descriptive commit message

### Before Creating PR

- [ ] All formatting checks pass
- [ ] All linting checks pass
- [ ] All tests pass
- [ ] Coverage maintained or improved
- [ ] Documentation updated
- [ ] Commit messages follow conventions

### CI/CD Must Pass

- [ ] Black formatting check
- [ ] Ruff linting check
- [ ] pytest test suite
- [ ] mypy type checking (zero-error gate: `uv run mypy src/`)

## Resources

### Official Documentation

- [Black Documentation](https://black.readthedocs.io/)
- [Ruff Documentation](https://docs.astral.sh/ruff/)
- [mypy Documentation](https://mypy.readthedocs.io/)
- [PEP 8 Style Guide](https://pep8.org/)

### Tool Comparisons

- [Why Ruff?](https://docs.astral.sh/ruff/#why-ruff) - Speed comparison
- [Black vs autopep8](https://black.readthedocs.io/en/stable/the_black_code_style/index.html)

### Cheat Sheets

**Quick Black:**
```bash
uv run black src/ tests/          # Format
uv run black --check src/         # Check only
uv run black --diff src/          # Show changes
```

**Quick Ruff:**
```bash
uv run ruff check src/            # Check
uv run ruff check src/ --fix      # Fix
uv run ruff check src/ --watch    # Watch mode
```

**All Checks:**
```bash
uv run black src/ tests/ && \
  uv run ruff check src/ --fix && \
  uv run pytest --cov
```

## Getting Help

- **Black Issues:** Check [Black FAQ](https://black.readthedocs.io/en/stable/faq.html)
- **Ruff Issues:** Check [Ruff Rules](https://docs.astral.sh/ruff/rules/)
- **Questions:** Open a [GitHub Issue](https://github.com/karotkriss/caffeinated-whale-cli/issues)

---

**Remember:** Consistent code quality makes the codebase easier to maintain and review. These tools automate most of the work!
