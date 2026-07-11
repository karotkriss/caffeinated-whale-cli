"""Tests for the ``cwcli apps`` command group (commands/apps.py) and the
``cwcli update`` deprecation + frappe special-case folded into commands/update.py.

Covers both modes for every subcommand per the captain standard:
  - interactive/human path and the non-interactive ``--json`` path,
  - non-TTY-without-flag refuses non-zero (destructive uninstall),
  - multi-bench-no-selector refuses,
  - multi-site fan-out with continue-and-report-all aggregation (one site fails ->
    non-zero, per-(app, site) report, no success banner),
  - git-URL install target name derivation,
  - post-mutation cache refresh through cache.recache_project,
  - ``frappe`` -> ``bench update --reset`` and the deprecated ``cwcli update`` alias.

The commands are Typer commands, so (like every other command test here) they are
called directly with ALL params explicit - an omitted Option leaks its truthy
default object.
"""

import json
import shlex
import types

import pytest
import typer

from caffeinated_whale_cli.commands import apps as apps_mod
from caffeinated_whale_cli.commands import update as update_mod
from caffeinated_whale_cli.commands import utils as cmd_utils

# ------------------------------------------------------------------- fake container


class _FakeAPI:
    """Streaming exec surface (client.api) used by apps._stream_bench / update."""

    def __init__(self, container):
        self.container = container
        self._pending = None

    def exec_create(self, cid, cmd, workdir=None, tty=False):
        self._pending = (cmd, workdir)
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=True, demux=False):
        cmd, workdir = self._pending
        code, out = self.container._run(cmd, workdir)
        self.container._last_code = code
        yield out.encode() if isinstance(out, str) else out

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.container._last_code}


class FakeFrappeContainer:
    """Records every exec, and serves programmable app/site listings + failures."""

    def __init__(self, *, available_apps=None, installed=None, fail_on=None):
        self.calls = []
        self.available_apps = available_apps if available_apps is not None else []
        self.installed = installed or {}  # site -> [app names]
        self.fail_on = fail_on or []  # substrings that make a command fail
        self.id = "cid"
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.client = types.SimpleNamespace(api=_FakeAPI(self))
        self._last_code = 0

    def reload(self):
        pass

    def _run(self, cmd, workdir=None):
        self.calls.append(cmd)
        for sub in self.fail_on:
            if sub in cmd:
                return 1, f"error running: {cmd}"
        if cmd.startswith("ls -1") and cmd.rstrip().endswith("/apps"):
            return 0, "\n".join(self.available_apps) + "\n"
        if "list-apps" in cmd:
            parts = shlex.split(cmd)
            site = parts[parts.index("--site") + 1] if "--site" in parts else ""
            return 0, "\n".join(self.installed.get(site, [])) + "\n"
        return 0, ""

    def exec_run(self, cmd, workdir=None, environment=None):
        code, out = self._run(cmd, workdir)
        return code, out.encode() if isinstance(out, str) else out


@pytest.fixture()
def wired(monkeypatch):
    """Patch the apps module's collaborators; return a small control object."""

    state = types.SimpleNamespace(
        recache_calls=[], bench="/workspace/frappe-bench", sites=["a.localhost", "b.localhost"]
    )

    monkeypatch.setattr(apps_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(apps_mod, "resolve_bench_path", lambda *a, **k: state.bench)
    monkeypatch.setattr(apps_mod.bench_sites, "list_sites", lambda *a, **k: list(state.sites))

    def fake_recache(project_name, verbose=False):
        state.recache_calls.append(project_name)
        return True

    monkeypatch.setattr(apps_mod.cache, "recache_project", fake_recache)

    # Bypass the @handle_docker_errors docker preflight on the apps commands.
    from caffeinated_whale_cli.utils import docker_utils

    monkeypatch.setattr(docker_utils.shutil, "which", lambda name: "/usr/bin/docker")

    class _Client:
        def ping(self):
            return True

    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: _Client())
    return state


def _install_container(fail_on=None):
    return FakeFrappeContainer(available_apps=["frappe", "payments"], fail_on=fail_on or [])


def _set_tty(monkeypatch, is_tty):
    class _Stdin:
        def isatty(self):
            return is_tty

    monkeypatch.setattr(cmd_utils.sys, "stdin", _Stdin())


# --------------------------------------------------------------------------- list


def test_list_available_apps_json(wired, monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "erpnext"])
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    apps_mod.list_apps(
        "proj",
        bench=None,
        bench_path=None,
        sites=[],
        installed=False,
        json_output=True,
        yes=False,
        verbose=False,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["available_apps"] == ["frappe", "erpnext"]
    assert "installed" not in out


def test_list_installed_all_sites_default(wired, monkeypatch, capsys):
    container = FakeFrappeContainer(
        available_apps=["frappe"],
        installed={"a.localhost": ["frappe", "payments"], "b.localhost": ["frappe"]},
    )
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    apps_mod.list_apps(
        "proj",
        bench=None,
        bench_path=None,
        sites=[],
        installed=True,
        json_output=True,
        yes=False,
        verbose=False,
    )
    out = json.loads(capsys.readouterr().out)
    # multi-site by default: installed reported for BOTH sites, grouped by site.
    assert set(out["installed"]) == {"a.localhost", "b.localhost"}
    assert out["installed"]["a.localhost"] == ["frappe", "payments"]


def test_list_multibench_no_selector_refuses(wired, monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe"])
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    def _ambiguous(*a, **k):
        raise typer.Exit(code=1)

    monkeypatch.setattr(apps_mod, "resolve_bench_path", _ambiguous)
    with pytest.raises(typer.Exit) as exc:
        apps_mod.list_apps(
            "proj",
            bench=None,
            bench_path=None,
            sites=[],
            installed=False,
            json_output=True,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1


# -------------------------------------------------------------------------- install


def test_install_all_sites_success_refreshes_cache(wired, monkeypatch, capsys):
    container = _install_container()
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    apps_mod.install_apps(
        "proj",
        ["payments"],
        bench=None,
        bench_path=None,
        sites=[],
        branch=None,
        fetch_only=False,
        json_output=True,
        yes=False,
        verbose=False,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    # get-app once + install-app on every site
    assert any("get-app" in c and "payments" in c for c in container.calls)
    installs = [c for c in container.calls if "install-app" in c]
    assert len(installs) == 2
    assert wired.recache_calls == ["proj"]


def test_install_one_site_fails_exits_nonzero_no_success(wired, monkeypatch, capsys):
    # Fail install-app only on b.localhost (continue-and-report-all aggregation).
    container = _install_container(fail_on=["--site b.localhost install-app"])
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.install_apps(
            "proj",
            ["payments"],
            bench=None,
            bench_path=None,
            sites=[],
            branch=None,
            fetch_only=False,
            json_output=True,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    per_site = {r["site"]: r["ok"] for r in out["results"] if r["action"] == "install-app"}
    assert per_site == {"a.localhost": True, "b.localhost": False}


def test_install_git_url_derives_app_name(wired, monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe"])
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    apps_mod.install_apps(
        "proj",
        ["https://github.com/example/custom_app.git"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        branch=None,
        fetch_only=False,
        json_output=True,
        yes=False,
        verbose=False,
    )
    # get-app gets the URL; install-app gets the derived basename (no .git).
    assert any("get-app" in c and "custom_app.git" in c for c in container.calls)
    assert any("install-app custom_app" in c for c in container.calls)


def test_install_fetch_only_skips_install(wired, monkeypatch):
    container = _install_container()
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    apps_mod.install_apps(
        "proj",
        ["payments"],
        bench=None,
        bench_path=None,
        sites=[],
        branch=None,
        fetch_only=True,
        json_output=True,
        yes=False,
        verbose=False,
    )
    assert any("get-app" in c for c in container.calls)
    assert not any("install-app" in c for c in container.calls)


# ------------------------------------------------------------------------ uninstall


def test_uninstall_json_without_yes_refuses(wired, monkeypatch):
    container = _install_container()
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.uninstall_apps(
            "proj",
            ["payments"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            remove_from_bench=False,
            json_output=True,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1
    assert not any("uninstall-app" in c for c in container.calls)


def test_uninstall_non_tty_without_yes_refuses(wired, monkeypatch):
    _set_tty(monkeypatch, False)
    container = _install_container()
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.uninstall_apps(
            "proj",
            ["payments"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            remove_from_bench=False,
            json_output=False,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1
    assert not any("uninstall-app" in c for c in container.calls)


def test_uninstall_yes_fans_out_and_refreshes(wired, monkeypatch, capsys):
    container = _install_container()
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    apps_mod.uninstall_apps(
        "proj",
        ["payments"],
        bench=None,
        bench_path=None,
        sites=[],
        remove_from_bench=False,
        json_output=True,
        yes=True,
        verbose=False,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    uninstalls = [c for c in container.calls if "uninstall-app" in c]
    assert len(uninstalls) == 2  # both sites
    assert all("--yes" in c for c in uninstalls)  # bench's own confirm suppressed
    assert wired.recache_calls == ["proj"]


def test_uninstall_remove_from_bench_deletes_dir(wired, monkeypatch):
    container = _install_container()
    monkeypatch.setattr(apps_mod, "get_frappe_container", lambda name: container)

    apps_mod.uninstall_apps(
        "proj",
        ["payments"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        remove_from_bench=True,
        json_output=True,
        yes=True,
        verbose=False,
    )
    assert any(c.startswith("rm -rf") and "apps/payments" in c for c in container.calls)


# ---------------------------------------------------------------------------- update


def test_apps_update_no_app_refuses(wired):
    with pytest.raises(typer.Exit) as exc:
        apps_mod.update_apps(
            "proj",
            None,
            bench=None,
            bench_path=None,
            sites=[],
            clear_cache=False,
            clear_website_cache=False,
            build=False,
            skip_maintenance=False,
            no_recache=False,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1


def test_apps_update_delegates_to_run_app_update(wired, monkeypatch):
    captured = {}

    def fake_run(project_name, apps, **kwargs):
        captured["project"] = project_name
        captured["apps"] = apps
        captured["sites"] = kwargs.get("sites")

    monkeypatch.setattr(apps_mod, "run_app_update", fake_run)
    apps_mod.update_apps(
        "proj",
        ["erpnext"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        clear_cache=False,
        clear_website_cache=False,
        build=False,
        skip_maintenance=False,
        no_recache=False,
        yes=False,
        verbose=False,
    )
    assert captured == {"project": "proj", "apps": ["erpnext"], "sites": ["a.localhost"]}


# ------------------------------------------------- update.py: frappe reset + deprecation


def _wire_update(monkeypatch, container):
    monkeypatch.setattr(
        update_mod, "ensure_containers_running", lambda *a, **k: True, raising=False
    )
    # _update_project imports ensure_containers_running/resolve_bench_path locally from .utils
    monkeypatch.setattr(cmd_utils, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(cmd_utils, "resolve_bench_path", lambda *a, **k: "/workspace/frappe-bench")
    monkeypatch.setattr(update_mod, "get_project_containers", lambda name: [container])
    monkeypatch.setattr(update_mod.cache, "recache_project", lambda *a, **k: True)


def test_update_frappe_runs_bench_update_reset(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe"])
    _wire_update(monkeypatch, container)

    update_mod._update_project("proj", ["frappe"], verbose=True)

    assert any("bench update --reset" in c for c in container.calls)
    assert not any("git pull" in c for c in container.calls)


def test_update_frappe_reset_failure_exits_nonzero(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe"], fail_on=["bench update --reset"])
    _wire_update(monkeypatch, container)

    with pytest.raises(typer.Exit) as exc:
        update_mod._update_project("proj", ["frappe"], verbose=True)
    assert exc.value.exit_code == 1


def test_deprecated_update_warns_and_delegates(monkeypatch, capsys):
    called = {}

    def fake_run(project_name, apps, **kwargs):
        called["apps"] = apps
        called["sites"] = kwargs.get("sites")

    monkeypatch.setattr(update_mod, "run_app_update", fake_run)

    # Bypass the @handle_docker_errors docker preflight (it reads from docker_utils).
    from caffeinated_whale_cli.utils import docker_utils

    monkeypatch.setattr(docker_utils.shutil, "which", lambda name: "/usr/bin/docker")

    class _Client:
        def ping(self):
            return True

    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: _Client())

    update_mod.update(
        "proj",
        apps=["erpnext"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        verbose=False,
        clear_cache=False,
        clear_website_cache=False,
        build=False,
        skip_maintenance=False,
        no_recache=False,
        yes=False,
    )
    err = capsys.readouterr().err
    assert "deprecated" in err.lower()
    assert called == {"apps": ["erpnext"], "sites": ["a.localhost"]}
