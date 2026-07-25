"""Packaging-realism full-lifecycle E2E - the runtime-deps-only leg.

Every OTHER E2E leg (and the whole local dev loop) drives the ``cwcli`` console
script out of a ``uv sync --all-extras`` dev venv, where dev/transitive deps are
present. That masks a whole class of packaging bug: a runtime ``import`` of a
package that is NOT declared in ``[project.dependencies]`` succeeds in the dev
venv and ships broken to a real ``uv tool install`` (exactly how the missing
``click`` runtime dependency reached 0.37.0 while every test passed).

The ``clean-install`` smoke job (test.yml) guards IMPORT-time deps, but only
over ``--help`` / ``config`` reads - it never runs a real Docker command path
(init/apps/backup/inspect/rm), so a dep imported lazily inside a command body
(a network client in init, a stream/archive path in backup) is invisible to it.

This leg closes that gap: the CI ``runtime-only`` job installs cwcli with
``uv tool install .`` (resolving ONLY runtime deps, no extras), points the
harness at that binary via ``CWCLI_BIN``, and runs one genuine full lifecycle -
init (fixture) -> apps list -> inspect -> backup -> rm (fixture teardown). Any
undeclared runtime dependency surfaces here as a ``ModuleNotFoundError`` on a
real command, which no other gate would catch.

The same installed binary also serves the recursive ``cwcli axi ... --help``
proof in this file. That test walks the source registry, invokes every matching
installed help path, and keeps the human Rich surface in the same artifact check.

Marked ``e2e_pkg`` (not ``e2e``) so it is OFF the per-version ``-m e2e`` matrix -
running it there would just double that leg's dominant init cost for no added
signal, since the matrix already uses the dev binary. Run locally with
``uv run pytest tests/e2e -m e2e_pkg -o addopts=""`` (drives the dev binary; the
CWCLI_BIN packaging axis is exercised in CI).
"""

from __future__ import annotations

import json
import shlex

import click
import pytest
from typer.main import get_command

from . import harness

pytestmark = pytest.mark.e2e_pkg


def _installed_axi_help_paths():
    """Enumerate the source registry that the runtime-only wheel must expose."""
    from caffeinated_whale_cli.main import app

    root = get_command(app)
    axi = root.commands["axi"]

    def walk(command, path):
        yield path, command
        if isinstance(command, click.Group):
            ctx = click.Context(command)
            for name in command.list_commands(ctx):
                child = command.get_command(ctx, name)
                assert child is not None
                yield from walk(child, [*path, name])

    yield from walk(axi, [])


def test_all_axi_help_is_toon_on_runtime_only_binary():
    """Every installed agent help path is complete TOON while human help stays Rich."""
    visited: list[tuple[str, ...]] = []
    for path, command in _installed_axi_help_paths():
        visited.append(tuple(path))
        result = harness.run_cwcli("axi", *path, "--help")

        # Prove useful content before checking the absence of decoration.
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.startswith('usage: "cwcli axi')
        assert "description:" in result.stdout
        assert "flags[" in result.stdout
        assert "examples[" in result.stdout
        arguments = [param for param in command.params if isinstance(param, click.Argument)]
        if arguments:
            assert "arguments[" in result.stdout
            for argument in arguments:
                assert argument.name in result.stdout
                assert argument.help in result.stdout
        if isinstance(command, click.Group):
            assert "commands[" in result.stdout
            assert "{name,description}" in result.stdout
            ctx = click.Context(command)
            for name in command.list_commands(ctx):
                assert name in result.stdout
                child = command.get_command(ctx, name)
                assert child is not None
                assert child.get_short_help_str() in result.stdout
        for option in (
            param
            for param in command.get_params(click.Context(command))
            if isinstance(param, click.Option) and not param.hidden
        ):
            assert option.opts[0] in result.stdout
            assert option.help in result.stdout

        assert not any("\u2500" <= char <= "\u257f" for char in result.stdout)
        assert "\x1b[" not in result.stdout
        assert result.stderr == ""
        assert all(line and line == line.rstrip() for line in result.stdout.splitlines())
        examples = next(line for line in result.stdout.splitlines() if line.startswith("examples["))
        assert ": " in examples

    assert () in visited
    assert ("scale",) in visited
    assert ("apps", "install") in visited

    init = harness.run_cwcli("axi", "init", "--help")
    init_examples = next(line for line in init.stdout.splitlines() if line.startswith("examples["))
    assert init_examples.count('CWCLI_ADMIN_PASSWORD=\\"<password>\\"') == 2

    human = harness.run_cwcli("scale", "--help")
    assert human.returncode == 0, human.stdout + human.stderr
    assert "Usage:" in human.stdout
    assert "Arguments" in human.stdout
    assert "--to" in click.unstyle(human.stdout)
    assert any("\u2500" <= char <= "\u257f" for char in human.stdout)
    assert not human.stdout.startswith("usage:")


def test_full_lifecycle_on_runtime_only_binary(running_instance, tmp_path):
    """init -> apps list -> inspect -> backup -> rm, each a real command path.

    Uses ``running_instance`` (a genuine ``cwcli init`` bench, torn down with
    ``cwcli rm`` on fixture teardown), so the whole lifecycle - including the two
    heaviest runtime-dep paths, init and rm - runs through whatever binary the
    harness is aimed at. Under the CI runtime-only job that binary carries only
    ``[project.dependencies]``, so an undeclared runtime import fails right here.
    """
    inst = running_instance

    # apps list --json: exercises core.list_apps + the TOON/JSON render path.
    apps = harness.run_cwcli("apps", "list", inst.name, "--json")
    assert apps.returncode == 0, apps.stdout + apps.stderr
    payload = json.loads(apps.stdout)
    assert payload, f"apps list --json returned empty payload: {apps.stdout!r}"

    # inspect: exercises the cache / db_utils (peewee/SQLite) read+write path.
    inspected = harness.run_cwcli("inspect", inst.name, "--yes")
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr

    # backup: exercises the in-container dump + get_archive stream-out path, and
    # produces a genuine artifact we copy out and prove non-empty on the host.
    backup = harness.run_cwcli("backup", inst.name, "--site", inst.site, "-y")
    assert backup.returncode == 0, backup.stdout + backup.stderr
    assert "Successfully created backup" in backup.stdout

    backups = f"{inst.bench}/sites/{inst.site}/private/backups"
    code, out = harness.exec_in_frappe(
        inst.name, f"ls -1t {shlex.quote(backups)}/*.sql.gz 2>/dev/null | head -1"
    )
    dump = out.strip()
    assert code == 0 and dump, "no *.sql.gz dump found in the container's backups dir"
    host_file = tmp_path / "pkg-lifecycle.sql.gz"
    assert harness.docker_cp_out(inst.name, dump, host_file), f"docker cp failed for {dump}"
    assert host_file.stat().st_size > 0, "DB dump copied to the host is empty"

    # rm (the destructive path) runs on fixture teardown, completing the lifecycle.
