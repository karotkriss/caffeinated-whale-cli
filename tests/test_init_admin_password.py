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

Re-pointed with its subjects by ``migrate-init-core``: the env-transport
assertions drive ``core.init_bench``'s command construction directly (the
transport lives in the core now); the generation, refusal, and print-once
gating stay command-level - they are the frontend's TTY-coupled secret UX.
Assertions are unchanged.
"""

import re
from types import SimpleNamespace

import pytest
import typer

import caffeinated_whale_cli.commands.init as init_mod
from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.utils import config_utils, db_utils


class _FakeApi:
    """Records each exec's command string and environment (success only)."""

    def __init__(self):
        self.exec_calls: list[dict] = []

    def exec_create(self, container_id, cmd, **kwargs):
        command = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) == 3 else " ".join(cmd)
        self.exec_calls.append({"command": command, "environment": kwargs.get("environment")})
        return {"Id": f"exec-{len(self.exec_calls)}"}

    def exec_start(self, exec_id, stream=False, demux=False, **kwargs):
        return iter([])

    def exec_inspect(self, exec_id):
        return {"ExitCode": 0, "Running": False}


class _FakeContainer:
    def __init__(self, site_exists=False):
        self.id = "fake-frappe"
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.client = SimpleNamespace(api=_FakeApi())
        self.site_exists = site_exists

    def reload(self):
        pass

    def exec_run(self, cmd, **kwargs):
        script = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) == 3 else ""
        if script.startswith("test -d") and "/sites/" in script:
            return (0 if self.site_exists else 1), b""
        if script.startswith("test -d"):
            return 1, b""  # no existing bench
        return 0, b""


def _run_core_init_bench(monkeypatch, *, admin_password, db_root_password="123"):
    """Drive the core's bench provisioning, recording each exec."""
    container = _FakeContainer()
    monkeypatch.setattr(core_init.core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
    monkeypatch.setattr(config_utils, "add_custom_path", lambda path: True)

    core_init.init_bench(
        "proj",
        bench_name="frappe-bench",
        site_name="development.localhost",
        bench_parent="/workspace",
        frappe_ref="version-16",
        db_root_password=db_root_password,
        admin_password=admin_password,
        erpnext_branch="version-16",
    )
    return container.client.api.exec_calls


def _run_init_command(monkeypatch, *, admin_password, interactive, site_created=True):
    """Drive the real ``init`` command body with both core stages stubbed,
    recording what the frontend hands to ``core.init_bench``.

    Returns the recorded ``init_bench`` kwargs (the generated password rides in
    ``admin_password`` there, never in the report).
    """
    recorded: dict = {}

    def fake_init_instance(project, **kwargs):
        return Result(status=Status.OK, data=core_init.InstanceUp(project=project, conf_dir="/x"))

    def fake_init_bench(project, **kwargs):
        recorded.update(kwargs)
        return Result(
            status=Status.OK,
            data=core_init.InitReport(
                project=project,
                bench_name=kwargs["bench_name"],
                bench_path=f"/workspace/{kwargs['bench_name']}",
                site_name=kwargs["site_name"],
                bench_created=True,
                site_created=site_created,
                erpnext_installed=False,
            ),
        )

    monkeypatch.setattr(init_mod.core_init, "init_instance", fake_init_instance)
    monkeypatch.setattr(init_mod.core_init, "init_bench", fake_init_bench)
    monkeypatch.setattr(init_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(init_mod, "_is_interactive_session", lambda: interactive)
    monkeypatch.setattr(init_mod.cache, "recache_project", lambda *a, **k: True)

    init_mod.init.__wrapped__(
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
    return recorded


def _new_site_call(calls):
    return next(c for c in calls if "bench new-site" in c["command"])


class TestNewSitePasswordTransport:
    """Both secrets ride in ``environment=`` and are referenced as ``$VAR``."""

    def test_passwords_are_env_refs_not_literals(self, monkeypatch):
        calls = _run_core_init_bench(monkeypatch, admin_password="s3cr3t-admin")
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

    def test_supplied_admin_password_used_verbatim(self, monkeypatch):
        calls = _run_core_init_bench(monkeypatch, admin_password="my-own-pw")
        env = _new_site_call(calls)["environment"]
        assert env["CWCLI_ADMIN_PASSWORD"] == "my-own-pw"

    def test_generated_admin_password_rides_off_argv(self, monkeypatch):
        # The frontend generates the password and hands it to the core, which
        # puts it in environment= only - never on the command string. The
        # command drive stubs the core stages, so its patches are scoped to
        # their own context before the real core call below.
        with pytest.MonkeyPatch.context() as mp:
            recorded = _run_init_command(mp, admin_password=None, interactive=True)
        pw = recorded["admin_password"]
        assert pw  # non-empty, generated
        assert pw != "admin"  # not the old hardcoded default

        calls = _run_core_init_bench(monkeypatch, admin_password=pw)
        call = _new_site_call(calls)
        assert call["environment"]["CWCLI_ADMIN_PASSWORD"] == pw
        assert pw not in call["command"]  # never on the argv


class TestNonInteractiveRefusal:
    def test_non_interactive_without_admin_password_refuses(self, monkeypatch):
        with pytest.raises(typer.Exit) as exc:
            _run_init_command(monkeypatch, admin_password=None, interactive=False)
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

    def test_printed_once_when_site_created(self, monkeypatch):
        printed = self._capture_console(monkeypatch)
        recorded = _run_init_command(
            monkeypatch, admin_password=None, interactive=True, site_created=True
        )
        pw = recorded["admin_password"]
        joined = "\n".join(printed)
        assert "Administrator password (generated)" in joined
        assert joined.count(pw) == 1
        # The "change it later" hint must be a copy-paste-accurate cwcli
        # invocation, not a raw `bench` command the user cannot run directly.
        assert (
            "`cwcli run proj --site development.localhost set-admin-password "
            "<new-password>`" in joined
        )

    def test_not_printed_on_idempotent_rerun(self, monkeypatch):
        # Site already exists -> new-site is skipped -> no password was set, so a
        # fresh generated one must NOT be printed (the correctness trap).
        printed = self._capture_console(monkeypatch)
        _run_init_command(monkeypatch, admin_password=None, interactive=True, site_created=False)
        joined = "\n".join(printed)
        assert "Administrator password (generated)" not in joined

    def test_supplied_password_never_echoed(self, monkeypatch):
        printed = self._capture_console(monkeypatch)
        _run_init_command(monkeypatch, admin_password="do-not-echo-me", interactive=True)
        joined = "\n".join(printed)
        assert "do-not-echo-me" not in joined
        assert "Administrator password (generated)" not in joined
