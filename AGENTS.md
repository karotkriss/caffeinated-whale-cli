# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## `init` command: Frappe/bench version gating

`bench new-site` MariaDB flag depends on the Frappe branch (see `_select_mariadb_flag` in `src/caffeinated_whale_cli/commands/init.py`):

- `version-13` and `version-14` -> `--no-mariadb-socket`
- `version-15`+ -> `--mariadb-user-host-login-scope=%` (this flag only exists in bench/Frappe 15+; passing it to bench 14 makes `bench new-site` fail)

Other per-branch settings nearby in `init.py`: Python version (`branch_python`: 15->3.12, 14->3.10, 13->3.9), Node major (`branch_node`: 14->16, 13->14), and `setuptools<82` is pinned only for `version-13`.

## CI quality gates

- `.github/workflows/lint.yml` runs `black --check` + `ruff check`.
- `.github/workflows/test.yml` runs `pytest` (with `pytest-cov`) and `mypy src/`. It triggers on pushes to all branches and PRs to all branches (the default branch is `develop`, not `master`).
- The `Pytest` job is the intended required gate. develop has no branch protection, so the admin must tick `Pytest` as a required status check in the develop branch-protection settings for it to actually block merges.
- The `Mypy (informational)` job is `continue-on-error: true` because mypy currently emits ~50 errors across ~14 files. Burn those errors down to zero, then drop `continue-on-error` and promote it to a required check.
- `build.yml` / `release.yml` trigger on `master` (the historical default); they build and publish to PyPI and do not run tests.

## `rm` command: what removal actually deletes

`docker-py`'s `Container.remove(v=True)` only removes a container's *anonymous* volumes. The named compose volumes that frappe-docker creates (e.g. `sites`, `db-data`) are NOT touched by it, so they must be removed explicitly or the databases/sites survive a "delete" (this was the issue #19 / "rm lies" bug).

In `src/caffeinated_whale_cli/commands/rm.py`:

- `_remove_named_volumes` enumerates volumes via `get_project_volumes` (label `com.docker.compose.project={name}` in `docker_utils.py`) and calls `volume.remove(force=True)`. It runs only when `remove_volumes` is true (`--volumes`, the default); `--no-volumes` leaves named volumes untouched.
- `_remove_project_directory` deletes the local cwcli instance dir `~/.cwcli/projects/{name}/` (`PROJECTS_DIR / name`). This runs regardless of `--no-volumes` because the directory is config, not data; leaving it behind was the lingering-project half of issue #19.
- Both run after the existing backup/archive safety step inside `_remove_project`, and the project dir is copied into the timestamped archive (`project_files/`) before deletion. Keep the confirmation text in `rm()` in sync with what is actually deleted.
