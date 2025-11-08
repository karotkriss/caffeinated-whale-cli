# Dependency Management with uv

This project uses [uv](https://docs.astral.sh/uv/) for fast, reliable dependency management.

## Understanding uv.lock

The `uv.lock` file is a **lockfile** that ensures reproducible builds across all environments.

### Key Concepts

**Lock File (`uv.lock`):**
- Contains exact versions of all dependencies and their transitive dependencies
- Includes checksums for security
- Should be committed to version control
- Ensures everyone gets the same dependencies

**Project File (`pyproject.toml`):**
- Contains dependency **specifications** (constraints)
- Example: `"ruff>=0.8.0"` (minimum version 0.8.0)
- Defines optional dependency groups (`[project.optional-dependencies]`)

---

## Dependency Groups

### Production Dependencies

```toml
[project]
dependencies = [
    "docker>=7.1.0",
    "typer>=0.16.0",
    "rich>=14.0.0",
    # ... other runtime deps
]
```

**Installed by:** All users who install the package

### Development Dependencies

```toml
[project.optional-dependencies]
dev = [
    "ruff>=0.8.0",      # Linter
    "mypy>=1.13.0",     # Type checker
    "pytest>=8.0.0",    # Testing framework
    "pytest-cov>=6.0.0", # Coverage reporting
]
```

**Installed by:** Contributors and CI/CD

---

## Common Commands

### Install Production Dependencies

```bash
# Install only runtime dependencies
uv sync
```

### Install All Dependencies (Including Dev)

```bash
# Install runtime + dev dependencies
uv sync --all-extras
```

### Update Dependencies

```bash
# Update dependencies to latest compatible versions
uv sync --upgrade

# Update specific package
uv sync --upgrade-package ruff
```

### Add New Dependency

```bash
# Add to production dependencies
uv add package-name

# Add to dev dependencies
uv add --dev package-name
```

---

## CI/CD Usage

### The `--frozen` Flag

In CI/CD workflows, we use `--frozen`:

```yaml
- name: Install dependencies
  run: uv sync --frozen --all-extras
```

**What `--frozen` does:**
- ✅ Uses exact versions from `uv.lock`
- ✅ Fails if `uv.lock` is out of sync with `pyproject.toml`
- ✅ Ensures reproducible builds
- ❌ Does NOT update `uv.lock`

**When to use:**
- ✅ CI/CD pipelines
- ✅ Production deployments
- ✅ When you want guaranteed reproducibility

**When NOT to use:**
- ❌ Adding new dependencies
- ❌ Updating dependencies
- ❌ Local development (optional)

---

## Workflow: Adding Dependencies

### 1. Add to `pyproject.toml`

**For runtime dependency:**
```bash
uv add docker
```

**For dev dependency:**
```bash
uv add --dev ruff
```

**Manual edit:**
```toml
[project.optional-dependencies]
dev = [
    "ruff>=0.8.0",  # Add this line
]
```

### 2. Update Lock File

```bash
# Sync will update uv.lock automatically
uv sync --all-extras
```

### 3. Verify Installation

```bash
uv run ruff --version
```

### 4. Commit Both Files

```bash
git add pyproject.toml uv.lock
git commit -m "build: add ruff for linting"
```

**⚠️ Important:** Always commit `uv.lock` when you change `pyproject.toml` dependencies!

---

## Why CI Failed: Common Issues

### Issue 1: Lock File Out of Sync

**Error in CI:**
```
error: The lockfile at `uv.lock` needs to be updated, but `--frozen` was provided.
```

**Cause:** Added dependency to `pyproject.toml` but didn't commit updated `uv.lock`

**Fix:**
```bash
uv sync --all-extras
git add uv.lock
git commit -m "build: update lock file"
```

---

### Issue 2: Missing `--all-extras`

**Error in CI:**
```
error: command not found: ruff
```

**Cause:** CI running `uv sync` without `--all-extras`, so dev dependencies not installed

**Fix:** Update workflow to use `uv sync --frozen --all-extras`

---

### Issue 3: Wrong Python Version

**Error in CI:**
```
error: Package `mypy` requires Python >=3.8, but 3.7 is installed
```

**Cause:** Python version mismatch

**Fix:** Check workflow uses correct Python version in container image:
```yaml
container:
  image: ghcr.io/astral-sh/uv:python3.12-bookworm
```

---

## Dependency Extras Explained

In `pyproject.toml`:
```toml
[project.optional-dependencies]
dev = ["ruff>=0.8.0"]
test = ["pytest>=8.0.0"]
docs = ["mkdocs>=1.5.0"]
```

**Install specific extra:**
```bash
uv sync --extra dev
uv sync --extra test
```

**Install multiple extras:**
```bash
uv sync --extra dev --extra test
```

**Install all extras:**
```bash
uv sync --all-extras
```

**In CI (our setup):**
```yaml
# Lint workflow needs dev tools
run: uv sync --frozen --all-extras

# Build workflow only needs runtime deps
run: uv sync --frozen
```

---

## Lock File Benefits

### Reproducibility

```bash
# Developer A (Jan 2024)
uv sync --frozen
# Installs: ruff==0.1.0

# Developer B (Mar 2024)
uv sync --frozen
# Installs: ruff==0.1.0  ✅ Same version!
```

Without lock file:
```bash
# Developer A (Jan 2024)
pip install ruff>=0.1.0
# Installs: ruff==0.1.0

# Developer B (Mar 2024)
pip install ruff>=0.1.0
# Installs: ruff==0.3.0  ❌ Different version!
```

### Security

Lock file includes checksums:
```toml
[[package]]
name = "ruff"
version = "0.14.3"
source = { registry = "https://pypi.org/simple" }
checksum = "..."  # Verifies package integrity
```

---

## Best Practices

### ✅ Do

- Commit `uv.lock` to version control
- Use `--frozen` in CI/CD
- Run `uv sync` after pulling changes
- Update lock file when adding dependencies
- Use `--all-extras` for dev/CI environments

### ❌ Don't

- Manually edit `uv.lock`
- Ignore lock file conflicts (resolve them properly)
- Use `--frozen` when adding new dependencies
- Forget to commit `uv.lock` changes
- Use `pip` alongside `uv` (pick one)

---

## Comparing to Other Tools

| Feature | uv | pip + requirements.txt | poetry |
|---------|----|-----------------------|--------|
| Speed | ⚡ Very fast | 🐌 Slow | 🏃 Fast |
| Lock file | ✅ Yes | ❌ No | ✅ Yes |
| Dependency resolution | ✅ Advanced | ⚠️ Basic | ✅ Advanced |
| Python installation | ✅ Built-in | ❌ No | ❌ No |
| Standard `pyproject.toml` | ✅ Yes | N/A | ⚠️ Custom fields |

---

## Troubleshooting

### Reset Lock File

If lock file is corrupted:
```bash
rm uv.lock
uv sync --all-extras
```

### Check What's Installed

```bash
uv pip list
```

### View Dependency Tree

```bash
uv pip tree
```

### Check for Outdated Packages

```bash
uv pip list --outdated
```

---

## Resources

- [uv Documentation](https://docs.astral.sh/uv/)
- [uv GitHub Repository](https://github.com/astral-sh/uv)
- [PEP 621 (pyproject.toml)](https://peps.python.org/pep-0621/)
- [PEP 508 (Dependency Specification)](https://peps.python.org/pep-0508/)
