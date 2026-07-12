"""Tests for ``init``'s admin-password hardening (M2).

Both secrets that ``bench new-site`` needs (the admin password and the MariaDB
root password) are routed off the argv: the command string references them as
unexpanded ``"$CWCLI_ADMIN_PASSWORD"`` / ``"$CWCLI_DB_ROOT_PASSWORD"`` and the
values ride in the ``environment`` dict, so they never appear in the process
list or the verbose echo (mirrors ``restore.py``'s M5 pattern).

The admin password is additionally generated when ``--admin-password`` is
omitted: printed once in an interactive run, refused in a non-interactive one.
The DB root password keeps its ``"123"`` default (coupled to the compose file)
but is still shielded off the argv.
"""

import re

import pytest
import typer

import caffeinated_whale_cli.commands.init as init_mod


class _FakeContainer:
    def exec_run(self, *a, **k):
        return 0, b""


def _run_init(
    monkeypatch,
    tmp_path,
    *,
    admin_password=None,
    interactive=True,
    site_exists=False,
    **overrides,
):
    """Drive the real ``init`` body to completion with every Docker/host
    boundary stubbed, recording each ``_exec_in_container`` call.

    Returns the list of ``{"command", ...kwargs}`` dicts (one per exec).
    """
    calls: list[dict] = []

    def recording_exec(container, command, **kwargs):
        calls.append({"command": command, **kwargs})

    monkeypatch.setattr(init_mod, "check_ports_in_use", lambda ports: dict.fromkeys(ports, False))
    monkeypatch.setattr(init_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(init_mod, "_setup_project_directory", lambda *a, **k: tmp_path)
    monkeypatch.setattr(init_mod, "_customize_compose_ports", lambda *a, **k: None)
    monkeypatch.setattr(init_mod, "_pull_compose_images", lambda *a, **k: None)
    monkeypatch.setattr(init_mod, "_start_compose_project", lambda *a, **k: None)
    monkeypatch.setattr(init_mod, "_wait_for_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(init_mod, "get_frappe_container", lambda *a, **k: _FakeContainer())
    monkeypatch.setattr(init_mod, "_ensure_directory", lambda *a, **k: None)
    monkeypatch.setattr(
        init_mod,
        "_resolve_bench_target",
        lambda *a, **k: ("frappe-bench", "/workspace/frappe-bench", False),
    )
    monkeypatch.setattr(init_mod, "_directory_exists", lambda *a, **k: site_exists)
    monkeypatch.setattr(init_mod.db_utils, "clear_cache_for_project", lambda *a, **k: None)
    monkeypatch.setattr(init_mod, "_is_interactive_session", lambda: interactive)
    monkeypatch.setattr(init_mod, "_exec_in_container", recording_exec)

    params = dict(
        project_name="proj",
        port=18500,
        bench_name="frappe-bench",
        site_name="development.localhost",
        bench_parent="/workspace",
        frappe_branch=None,
        version=None,
        db_root_password="123",
        admin_password=admin_password,
        auto_start=False,
        reuse_bench=None,
        verbose=False,
        install_erpnext=False,
        erpnext_branch="version-16",
    )
    params.update(overrides)

    init_mod.init.__wrapped__(**params)
    return calls


def _new_site_call(calls):
    return next(c for c in calls if "bench new-site" in c["command"])


class TestNewSitePasswordTransport:
    """Both secrets ride in ``environment=`` and are referenced as ``$VAR``."""

    def test_passwords_are_env_refs_not_literals(self, monkeypatch, tmp_path):
        calls = _run_init(monkeypatch, tmp_path, admin_password="s3cr3t-admin")
        call = _new_site_call(calls)
        cmd = call["command"]

        assert '"$CWCLI_ADMIN_PASSWORD"' in cmd
        assert '"$CWCLI_DB_ROOT_PASSWORD"' in cmd
        # The literal secret values must never touch the command string.
        assert "s3cr3t-admin" not in cmd
        assert "123" not in cmd

        env = call["environment"]
        assert env["CWCLI_ADMIN_PASSWORD"] == "s3cr3t-admin"
        assert env["CWCLI_DB_ROOT_PASSWORD"] == "123"

    def test_supplied_admin_password_used_verbatim(self, monkeypatch, tmp_path):
        calls = _run_init(monkeypatch, tmp_path, admin_password="my-own-pw")
        env = _new_site_call(calls)["environment"]
        assert env["CWCLI_ADMIN_PASSWORD"] == "my-own-pw"

    def test_generated_admin_password_rides_in_env_off_argv(self, monkeypatch, tmp_path):
        calls = _run_init(monkeypatch, tmp_path, admin_password=None, interactive=True)
        call = _new_site_call(calls)
        pw = call["environment"]["CWCLI_ADMIN_PASSWORD"]
        assert pw  # non-empty, generated
        assert pw != "admin"  # not the old hardcoded default
        assert pw not in call["command"]  # never on the argv


class TestNonInteractiveRefusal:
    def test_non_interactive_without_admin_password_refuses(self, monkeypatch, tmp_path):
        with pytest.raises(typer.Exit) as exc:
            _run_init(monkeypatch, tmp_path, admin_password=None, interactive=False)
        assert exc.value.exit_code == 1


class TestGenerator:
    def test_generator_nonempty_distinct_and_shell_safe(self):
        a = init_mod._generate_admin_password()
        b = init_mod._generate_admin_password()
        assert a and b
        assert a != b
        # token_urlsafe charset only, so it never needs shell quoting.
        assert re.fullmatch(r"[A-Za-z0-9_-]+", a)
        assert re.fullmatch(r"[A-Za-z0-9_-]+", b)


class TestGeneratedPasswordPrint:
    """The generated password prints exactly once, only when a site was made."""

    def _capture_console(self, monkeypatch):
        printed: list[str] = []
        monkeypatch.setattr(
            init_mod.console,
            "print",
            lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        )
        return printed

    def test_printed_once_when_site_created(self, monkeypatch, tmp_path):
        printed = self._capture_console(monkeypatch)
        calls = _run_init(monkeypatch, tmp_path, admin_password=None, interactive=True)
        pw = _new_site_call(calls)["environment"]["CWCLI_ADMIN_PASSWORD"]
        joined = "\n".join(printed)
        assert "Administrator password (generated)" in joined
        assert joined.count(pw) == 1

    def test_not_printed_on_idempotent_rerun(self, monkeypatch, tmp_path):
        # Site already exists -> new-site is skipped -> no password was set, so a
        # fresh generated one must NOT be printed (the correctness trap).
        printed = self._capture_console(monkeypatch)
        _run_init(monkeypatch, tmp_path, admin_password=None, interactive=True, site_exists=True)
        joined = "\n".join(printed)
        assert "Administrator password (generated)" not in joined

    def test_supplied_password_never_echoed(self, monkeypatch, tmp_path):
        printed = self._capture_console(monkeypatch)
        _run_init(monkeypatch, tmp_path, admin_password="do-not-echo-me")
        joined = "\n".join(printed)
        assert "do-not-echo-me" not in joined
        assert "Administrator password (generated)" not in joined
