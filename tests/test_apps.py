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
from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import exec_stream as exec_stream_mod
from caffeinated_whale_cli.core import update as core_update

# ------------------------------------------------------------------- fake container


class _FakeAPI:
    """Streaming exec surface (client.api) used by apps._stream_bench / update."""

    def __init__(self, container):
        self.container = container
        self._pending = None

    def exec_create(self, cid, cmd, workdir=None, tty=False, environment=None):
        self._pending = (cmd, workdir)
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=True, demux=False):
        cmd, workdir = self._pending
        code, out = self.container._run(cmd, workdir)
        self.container._last_code = code
        raw = out.encode() if isinstance(out, str) else out
        # Honour demux the way docker-py does: demuxed streams yield
        # (stdout, stderr) pairs, non-demuxed ones yield raw bytes.
        yield (raw, None) if demux else raw

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.container._last_code}


class FakeFrappeContainer:
    """Records every exec, and serves programmable app/site listings + failures."""

    def __init__(self, *, available_apps=None, installed=None, fail_on=None, get_app_creates=None):
        self.calls = []
        self.available_apps = available_apps if available_apps is not None else []
        self.installed = installed or {}  # site -> [app names]
        self.fail_on = fail_on or []  # substrings that make a command fail
        self.get_app_creates = get_app_creates or {}  # target substring -> apps/ dirname
        self.id = "cid"
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.client = types.SimpleNamespace(api=_FakeAPI(self))
        self._last_code = 0

    def reload(self):
        pass

    def _run(self, cmd, workdir=None):
        # cmd may be a str or a list form (e.g. _dir_exists' ["sh", "-c", ...]);
        # record a normalized string so every calls-based assertion (in / == /
        # startswith) works uniformly.
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(cmd_str)
        for sub in self.fail_on:
            if sub in cmd_str:
                return 1, f"error running: {cmd_str}"
        if cmd_str.startswith("bench get-app"):
            for target, dirname in self.get_app_creates.items():
                if target in cmd_str and dirname not in self.available_apps:
                    self.available_apps.append(dirname)
                    break
            return 0, ""
        if cmd_str.strip() == "git remote":
            return 0, "upstream\n"
        if cmd_str.startswith("ls -1") and cmd_str.rstrip().endswith("apps"):
            # Matches both "ls -1 <bench>/apps" and the workdir form "ls -1 apps".
            return 0, "\n".join(self.available_apps) + "\n"
        if "list-apps" in cmd_str:
            parts = shlex.split(cmd_str)
            site = parts[parts.index("--site") + 1] if "--site" in parts else ""
            return 0, "\n".join(self.installed.get(site, [])) + "\n"
        return 0, ""

    def exec_run(self, cmd, workdir=None, **kwargs):
        # Accepts environment=/demux= etc.; the non-verbose _stream_command path
        # passes demux=False.
        code, out = self._run(cmd, workdir)
        return code, out.encode() if isinstance(out, str) else out


def _wire_stopped_bench(monkeypatch):
    """No manager runs for this bench, so no post-mutation resync happens.

    The default for every fake here: `core.supervision.resync_after_code_change`
    reads process state with ``required=True`` and RAISES on an unreadable read
    (fail-honest), so a fake that answers no ``ps`` would fail every mutating verb
    rather than exercise it. Patching the two discovery entry points on the shared
    ``supervision`` module covers ``core.apps`` and ``core.update`` at once - they
    import the same module object.
    """
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_stack",
        lambda *a, **k: core_apps.supervision.StackSnapshot(
            supervisor_up=False, supervisor_pid=None, processes=[]
        ),
    )
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_unsupervised_stack",
        lambda *a, **k: core_apps.supervision.UnsupervisedStack(manager_up=False, processes=[]),
    )


@pytest.fixture()
def wired(monkeypatch):
    """Patch the apps module's collaborators; return a small control object."""

    state = types.SimpleNamespace(
        recache_calls=[], bench="/workspace/frappe-bench", sites=["a.localhost", "b.localhost"]
    )

    monkeypatch.setattr(apps_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(apps_mod, "resolve_bench_path", lambda *a, **k: state.bench)
    monkeypatch.setattr(core_apps.bench_sites, "list_sites", lambda *a, **k: list(state.sites))
    _wire_stopped_bench(monkeypatch)

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
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

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
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

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
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

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


def test_list_installed_read_failure_exits_nonzero_json(wired, monkeypatch, capsys):
    container = FakeFrappeContainer(
        available_apps=["frappe"],
        installed={"a.localhost": ["frappe"]},
        fail_on=["--site b.localhost list-apps"],
    )
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    with pytest.raises(typer.Exit) as exc:
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
    assert exc.value.exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["installed"]["a.localhost"] == ["frappe"]
    assert out["installed"]["b.localhost"] is None


def test_list_installed_read_failure_exits_nonzero_human(wired, monkeypatch, capsys):
    container = FakeFrappeContainer(
        available_apps=["frappe"],
        installed={"a.localhost": ["frappe"]},
        fail_on=["--site b.localhost list-apps"],
    )
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.list_apps(
            "proj",
            bench=None,
            bench_path=None,
            sites=[],
            installed=True,
            json_output=False,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "could not read installed apps" in err


# -------------------------------------------------------------------------- install


def test_install_all_sites_success_refreshes_cache(wired, monkeypatch, capsys):
    container = _install_container()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

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
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

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
    container = FakeFrappeContainer(
        available_apps=["frappe"],
        get_app_creates={"custom_app.git": "custom_app"},
    )
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

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
    # get-app gets the URL; install-app gets the name detected under apps/.
    assert any("get-app" in c and "custom_app.git" in c for c in container.calls)
    assert any("install-app custom_app" in c for c in container.calls)


def test_install_git_url_uses_detected_dir_over_url_basename(wired, monkeypatch):
    # The repo/basename ("hrms_custom") differs from the actual app package dir
    # that get-app creates under apps/ ("hrms") - a renamed fork/mirror scenario.
    container = FakeFrappeContainer(
        available_apps=["frappe"],
        get_app_creates={"hrms_custom.git": "hrms"},
    )
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    apps_mod.install_apps(
        "proj",
        ["https://github.com/example/hrms_custom.git"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        branch=None,
        fetch_only=False,
        json_output=True,
        yes=False,
        verbose=False,
    )
    assert any("install-app hrms" in c for c in container.calls)
    assert not any("install-app hrms_custom" in c for c in container.calls)


def test_install_falls_back_to_derived_name_when_no_new_dir(wired, monkeypatch):
    # get-app adds nothing new under apps/ (e.g. already present); fall back to
    # parsing the target instead of failing to resolve a name at all.
    container = FakeFrappeContainer(available_apps=["frappe"])
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    apps_mod.install_apps(
        "proj",
        ["custom_app"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        branch=None,
        fetch_only=False,
        json_output=True,
        yes=False,
        verbose=False,
    )
    assert any("install-app custom_app" in c for c in container.calls)


def test_install_fetch_only_skips_install(wired, monkeypatch):
    container = _install_container()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

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


def test_install_fetch_only_banner_says_fetched(wired, monkeypatch, capsys):
    # The success banner must not claim "installed" when nothing was installed.
    container = _install_container()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    apps_mod.install_apps(
        "proj",
        ["payments"],
        bench=None,
        bench_path=None,
        sites=[],
        branch=None,
        fetch_only=True,
        json_output=False,
        yes=False,
        verbose=False,
    )
    out = capsys.readouterr().out.lower()
    assert "fetched" in out
    assert "installed" not in out


# ------------------------------------------------------------------------ uninstall


def test_uninstall_json_without_yes_refuses(wired, monkeypatch):
    container = _install_container()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.uninstall_apps(
            "proj",
            ["payments"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            json_output=True,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1
    assert not any("uninstall-app" in c for c in container.calls)


def test_uninstall_non_tty_without_yes_refuses(wired, monkeypatch):
    _set_tty(monkeypatch, False)
    container = _install_container()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.uninstall_apps(
            "proj",
            ["payments"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            json_output=False,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1
    assert not any("uninstall-app" in c for c in container.calls)


def test_uninstall_yes_fans_out_and_refreshes(wired, monkeypatch, capsys):
    container = _install_container()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    apps_mod.uninstall_apps(
        "proj",
        ["payments"],
        bench=None,
        bench_path=None,
        sites=[],
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


# -------------------------------------------------------------------------- checkout


def test_checkout_fetches_and_checks_out_the_ref_and_refreshes(wired, monkeypatch, capsys):
    container = _install_container()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    apps_mod.checkout_app(
        "proj",
        "payments",
        "feature/x",
        bench=None,
        bench_path=None,
        reset=False,
        json_output=True,
        yes=False,
        verbose=False,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert [r["action"] for r in out["results"]] == ["fetch", "checkout"]
    assert "git fetch upstream -- feature/x" in container.calls
    assert "git checkout -B feature/x FETCH_HEAD" in container.calls
    assert not any("reset --hard" in c for c in container.calls)
    assert wired.recache_calls == ["proj"]  # git state changed -> cache refreshed


def test_checkout_reset_adds_hard_reset(wired, monkeypatch, capsys):
    container = _install_container()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    apps_mod.checkout_app(
        "proj",
        "payments",
        "v1.2.0",
        bench=None,
        bench_path=None,
        reset=True,
        json_output=True,
        yes=False,
        verbose=False,
    )
    out = json.loads(capsys.readouterr().out)
    assert [r["action"] for r in out["results"]] == ["fetch", "checkout", "reset"]
    assert "git reset --hard FETCH_HEAD" in container.calls


def test_checkout_failed_fetch_exits_nonzero_and_skips_the_rest(wired, monkeypatch, capsys):
    container = _install_container(fail_on=["git fetch"])
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.checkout_app(
            "proj",
            "payments",
            "feature/x",
            bench=None,
            bench_path=None,
            reset=False,
            json_output=True,
            yes=False,
            verbose=False,
        )
    assert exc.value.exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert [r["action"] for r in out["results"]] == ["fetch"]
    assert not any("git checkout" in c for c in container.calls)
    assert wired.recache_calls == []  # nothing changed -> no refresh


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
    # run_app_update imports ensure_containers_running/resolve_bench_path locally
    # from .utils: that interactive prologue stays in the frontend (prompts must
    # happen before any spinner), so it is still patched there...
    monkeypatch.setattr(
        update_mod, "ensure_containers_running", lambda *a, **k: True, raising=False
    )
    monkeypatch.setattr(cmd_utils, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(cmd_utils, "resolve_bench_path", lambda *a, **k: "/workspace/frappe-bench")
    # ...while container resolution and the recache moved into core.update. The fake
    # container already satisfies resolve_container_state (it reloads and reports
    # "running"), so only the accessor needs replacing.
    monkeypatch.setattr(core_update.core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(core_update.cache, "recache_project", lambda *a, **k: True)
    _wire_stopped_bench(monkeypatch)


def test_update_frappe_runs_bench_update_reset(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe"])
    _wire_update(monkeypatch, container)

    update_mod.run_app_update("proj", ["frappe"], verbose=True)

    assert any("bench update --reset" in c for c in container.calls)
    assert not any("git pull" in c for c in container.calls)


class _NoisyFrappeContainer(FakeFrappeContainer):
    """A container whose ``bench update --reset`` actually emits output.

    The base fake returns "" for that command, which is exactly why the hardcoded
    ``verbose=True`` was invisible: with no bytes to stream, streaming and not
    streaming look identical.
    """

    RESET_OUTPUT = "Updating apps...\n✓ frappe updated\nMigrating site...\n"

    def _run(self, cmd, workdir=None):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        if "bench update --reset" in cmd_str:
            self.calls.append(cmd_str)
            return 0, self.RESET_OUTPUT
        return super()._run(cmd, workdir)


def test_update_frappe_reset_writes_no_bench_output_to_stdout_when_not_verbose(monkeypatch, capsys):
    # _run_frappe_update_reset used to call _stream_command with verbose HARDCODED
    # True, so `apps update <proj> --app frappe` wrote bench output to stdout
    # whatever the caller asked for. That is the concrete blocker for a structured
    # surface (--json / axi), whose stdout must carry exactly one document - so this
    # pins the property rather than the implementation.
    #
    # All three sibling frappe tests pass verbose=True and therefore cannot see it.
    container = _NoisyFrappeContainer(available_apps=["frappe"])
    _wire_update(monkeypatch, container)

    update_mod.run_app_update("proj", ["frappe"], verbose=False)

    out = capsys.readouterr().out
    assert "Updating apps" not in out
    assert "Migrating site" not in out
    # The reset still ran, and the human still gets the banner.
    assert any("bench update --reset" in c for c in container.calls)
    assert "Frappe framework updated" in " ".join(out.split())


def test_update_frappe_reset_streams_bench_output_when_verbose(monkeypatch, capsys):
    # The other half of the contract: verbose still RENDERS. `verbose` decides
    # whether to render, never how to obtain (the exec-stream contract).
    container = _NoisyFrappeContainer(available_apps=["frappe"])
    _wire_update(monkeypatch, container)

    update_mod.run_app_update("proj", ["frappe"], verbose=True)

    assert "Updating apps" in capsys.readouterr().out


def test_update_frappe_reset_failure_exits_nonzero(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe"], fail_on=["bench update --reset"])
    _wire_update(monkeypatch, container)

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["frappe"], verbose=True)
    assert exc.value.exit_code == 1


def test_update_frappe_announces_ignored_per_app_flags(monkeypatch, capsys):
    # bench update --reset is bench-wide, so per-app/per-site flags do not apply and
    # must be reported as ignored (not silently dropped).
    container = FakeFrappeContainer(available_apps=["frappe"])
    _wire_update(monkeypatch, container)

    update_mod.run_app_update(
        "proj", ["frappe"], verbose=True, clear_cache=True, sites=["a.localhost"]
    )

    err = capsys.readouterr().err.lower()
    assert "ignoring" in err
    assert "--clear-cache" in err
    assert "--site" in err
    assert any("bench update --reset" in c for c in container.calls)


def _wire_update_site_filter(
    monkeypatch, container, *, installed_apps, bench_path="/workspace/frappe-bench"
):
    _wire_update(monkeypatch, container)
    cached = {
        "bench_instances": [
            {
                "path": bench_path,
                "sites": [
                    {"name": site, "installed_apps": apps} for site, apps in installed_apps.items()
                ],
            }
        ]
    }
    monkeypatch.setattr(
        core_update.db_utils, "get_cached_project_data", lambda project_name: cached
    )


def test_update_site_filter_matches_no_affected_site_refuses(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update_site_filter(monkeypatch, container, installed_apps={"a.localhost": ["payments"]})

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=True, sites=["b.localhost"])
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "matched no affected site" in err.lower()
    assert not any("migrate" in c for c in container.calls)


def test_update_site_filter_matches_affected_site_succeeds(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update_site_filter(monkeypatch, container, installed_apps={"a.localhost": ["payments"]})

    update_mod.run_app_update("proj", ["payments"], verbose=True, sites=["a.localhost"])
    assert any("bench --site a.localhost migrate" in c for c in container.calls)


# ---------------------------- update.py: verbose/non-verbose restructure (u4 + b8)


# The site-discovery / recache / sleep internals moved from commands/update.py into
# core/update.py with the state machine (openspec `migrate-update-core`). Every test
# patches them through these helpers rather than reaching for the module attribute
# directly, so the move re-pointed ONLY the four helpers below - no test's call or
# assertion changed.


def _count_discovery(monkeypatch, sites):
    """Patch discovery to a counter that returns ``sites``; return the counter dict."""
    counter = {"n": 0}

    def counting(*a, **k):
        counter["n"] += 1
        return list(sites)

    monkeypatch.setattr(core_update, "_sites_with_app", counting)
    return counter


def _patch_discovery_per_app(monkeypatch, per_app):
    """Patch discovery to return a different site list per app name."""

    def per_app_sites(_project, _bench_path, app, *a, **k):
        return list(per_app[app])

    monkeypatch.setattr(core_update, "_sites_with_app", per_app_sites)


def _record_recache(monkeypatch):
    """Patch the post-pull recache to a recorder; return the list of project names."""
    calls = []

    def fake_recache(project_name, verbose=False):
        calls.append(project_name)
        return True

    monkeypatch.setattr(core_update.cache, "recache_project", fake_recache)
    return calls


def _no_sleep(monkeypatch):
    """Drop the post-migration lock-settle sleep: real behaviour, pure test latency."""
    monkeypatch.setattr(core_update.time, "sleep", lambda *_a, **_k: None)


@pytest.mark.parametrize("verbose", [True, False])
def test_update_pulls_and_discovers_once(monkeypatch, verbose):
    # Both paths must git pull once per app and discover once per app - no double
    # pull, no second discovery pass (the u4 restructure / b8 mis-bound-else fix).
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    discovery = _count_discovery(monkeypatch, ["a.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=verbose)

    assert sum(1 for c in container.calls if c == "git pull") == 1
    assert discovery["n"] == 1  # one app -> exactly one discovery pass
    assert any("bench --site a.localhost migrate" in c for c in container.calls)


@pytest.mark.parametrize("verbose", [True, False])
def test_update_empty_affected_set_performs_neither_second_pass(monkeypatch, verbose):
    # b8: an empty affected-set used to re-run git pull + discovery via the
    # mis-bound else. It must now do neither a second time, and never migrate.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    discovery = _count_discovery(monkeypatch, [])  # no site has the app

    update_mod.run_app_update("proj", ["payments"], verbose=verbose)

    assert sum(1 for c in container.calls if c == "git pull") == 1
    assert discovery["n"] == 1
    assert not any("migrate" in c for c in container.calls)
    assert not any("set-maintenance-mode" in c for c in container.calls)


def test_update_migrate_skipped_for_site_not_in_maintenance(monkeypatch, capsys):
    # A site whose maintenance-enable fails is NOT migrated (never migrate a site
    # we could not put into maintenance) and is NOT cache/lock-cleared either (it
    # was never actually updated); this is surfaced in the summary and forces a
    # non-zero exit, while a healthy sibling still migrates and clears normally.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    container.fail_on = ["--site b.localhost set-maintenance-mode on"]
    _count_discovery(monkeypatch, ["a.localhost", "b.localhost"])

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=True, clear_cache=True)
    assert exc.value.exit_code == 1

    assert any("bench --site a.localhost migrate" in c for c in container.calls)
    assert not any("bench --site b.localhost migrate" in c for c in container.calls)
    assert any("bench --site a.localhost clear-cache" in c for c in container.calls)
    assert not any("bench --site b.localhost clear-cache" in c for c in container.calls)
    assert not any("b.localhost/locks" in c for c in container.calls if c.startswith("rm -rf"))

    out = capsys.readouterr().out
    assert "b.localhost" in out
    assert "could not enter maintenance mode" in out


def test_update_stuck_site_warns_and_exits_nonzero(monkeypatch, capsys):
    # A failed maintenance-disable leaves a site stuck -> warn + non-zero exit.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    container.fail_on = ["set-maintenance-mode off"]  # disable always fails
    _count_discovery(monkeypatch, ["a.localhost"])

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=True)
    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "a.localhost" in out
    assert "set-maintenance-mode off" in out


def _lose_stream_on(container, needle):
    """Make the exec whose command contains ``needle`` look like a dropped connection.

    The exact shape Docker reports while an exec is still running: ``ExitCode`` is
    present but None, so ``.get("ExitCode", 1)``'s default never fires, and
    CancellableStream has already turned the dropped socket into a silent
    StopIteration - so core.exec_stream cannot tell it from a clean EOF, polls, and
    raises CwcliError rather than guessing an outcome.
    """
    api = container.client.api
    real_create = api.exec_create
    api._lost = False

    def exec_create(cid, cmd, workdir=None, tty=False, environment=None):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        api._lost = needle in cmd_str
        return real_create(cid, cmd, workdir=workdir, tty=tty, environment=environment)

    def exec_inspect(exec_id):
        return (
            {"ExitCode": None, "Running": True} if api._lost else {"ExitCode": container._last_code}
        )

    api.exec_create = exec_create
    api.exec_inspect = exec_inspect


@pytest.mark.parametrize("verbose", [True, False])
def test_update_stream_loss_mid_fanout_still_reports_stuck_site_remediation(
    monkeypatch, capsys, verbose
):
    # REGRESSION (batch 3 / PR #80): reporting used to live AFTER the try/finally, so
    # exec_stream's CwcliError (which _stream_command turned into typer.Exit) unwound
    # straight past the seven-way summary, and a site left genuinely stuck lost the
    # one line telling the user how to unstick it. PR #81 fixed that by reporting
    # from the finally; the report is now a RETURNED value, so it cannot be skipped
    # at all.
    #
    # CHANGED BY DESIGN (openspec migrate-update-core, Decision 2): a lost stream no
    # longer ABORTS the fan-out. It used to, while a migration that merely returned
    # non-zero was recorded and the fan-out continued - the same real-world event,
    # two behaviours, decided by whether Docker happened to record an exit code. Now
    # both continue, and the lost one is reported as UNKNOWN rather than failed,
    # because it may still be running and an agent must not retry it blindly.
    #
    # Drive the dropped-connection shape into a.localhost's migrate, with the disable
    # failing too, so the site really is stuck and really needs the line. Do not spend
    # the real 10s poll bound waiting for a code that never comes.
    monkeypatch.setattr(exec_stream_mod, "_EXIT_CODE_POLL_TIMEOUT", 0.01)

    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    container.fail_on = ["set-maintenance-mode off"]  # every disable fails -> stuck
    _lose_stream_on(container, "bench --site a.localhost migrate")
    _count_discovery(monkeypatch, ["a.localhost", "b.localhost"])

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=verbose)

    # The honest exit code survives: an unknown outcome is never a success.
    assert exc.value.exit_code == 1
    # rich hard-wraps to the console width, so collapse whitespace before matching
    # any line long enough to be broken (the remediation command is).
    out = " ".join(capsys.readouterr().out.split())

    # The summary is reachable, and the stuck site keeps its actionable remediation,
    # for BOTH sites the finally tried and failed to take back out of maintenance.
    assert "Update completed with errors" in out
    assert "cwcli run proj --site a.localhost set-maintenance-mode off" in out
    assert "cwcli run proj --site b.localhost set-maintenance-mode off" in out

    # The lost site is reported as UNKNOWN, not as a failure: retrying a migration
    # that may still be running is harmful, so the two must stay distinguishable.
    assert "Lost track of 1 site(s)" in out
    assert "a.localhost: its migration may still be running; check before retrying" in out
    assert "a.localhost: Migration failed" not in out

    # The fan-out CONTINUED: b.localhost was migrated rather than abandoned, which is
    # the behaviour a non-zero exit code has always produced.
    assert any("bench --site b.localhost migrate" in c for c in container.calls)
    assert "stopped before completion" not in out

    # The invariant that already held keeps holding: maintenance was enabled for both
    # sites and a disable was ATTEMPTED for both.
    assert any("bench --site a.localhost set-maintenance-mode on" in c for c in container.calls)
    assert any("bench --site b.localhost set-maintenance-mode off" in c for c in container.calls)


def test_update_site_filter_refusal_is_not_reported_as_an_interrupted_update(monkeypatch, capsys):
    # The abort line must only fire once there was a fan-out to abandon. A --site
    # typo raises before any site is touched, so the refusal's own error stands
    # alone rather than being dressed up as an update that stopped halfway.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _count_discovery(monkeypatch, ["a.localhost"])

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=True, sites=["nope.local"])

    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert "--site matched no affected site(s)" in captured.err
    assert "stopped before completion" not in captured.out
    assert "Update completed with errors" not in captured.out


def test_update_shell_interpolations_are_shlex_quoted(monkeypatch):
    # Every interpolated site/path rides through shlex.quote, so a shell-special
    # site name is passed as one safe token, never word-split or injected.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _count_discovery(monkeypatch, ["weird site"])

    update_mod.run_app_update("proj", ["payments"], verbose=True, clear_cache=True)

    assert any("bench --site 'weird site' set-maintenance-mode on" in c for c in container.calls)
    assert any("bench --site 'weird site' migrate" in c for c in container.calls)
    assert any("bench --site 'weird site' clear-cache" in c for c in container.calls)
    assert any("'/workspace/frappe-bench/sites/weird site/locks'" in c for c in container.calls)


def test_stuck_site_remediation_line_is_shlex_quoted(monkeypatch, capsys):
    # The above test only asserts on container.calls (the exec), which is why it
    # stayed green while the PRINTED remediation line lost its shlex.quote and
    # became a broken, unquotable copy-paste command - exactly the message whose
    # whole purpose is to let a human rescue a site left stuck in maintenance mode.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _count_discovery(monkeypatch, ["weird site"])
    container.fail_on = ["set-maintenance-mode off"]

    with pytest.raises(typer.Exit):
        update_mod.run_app_update("proj", ["payments"], verbose=True)

    out = " ".join(capsys.readouterr().out.split())
    assert "cwcli run proj --site 'weird site' set-maintenance-mode off" in out


# ------------------------------------------- narration lines the table-driven
# ------------------------------------ renderer rewrite silently dropped
#
# The characterization tests above only ever asserted on failure/summary text and
# exec calls, never on this narration - which is exactly why they stayed green
# while it vanished. These pin it back, with its original gating: item (1) is
# UNCONDITIONAL, the rest are VERBOSE-ONLY (and asserted absent when not verbose).


def test_maintenance_enabled_count_prints_unconditionally_and_counts_successes(monkeypatch, capsys):
    # Not verbose-gated: the default path must still show that live sites went
    # down before migrations start. N is who SUCCEEDED, not who was attempted.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost", "b.localhost"])
    container.fail_on = ["--site b.localhost set-maintenance-mode on"]

    with pytest.raises(typer.Exit):
        update_mod.run_app_update("proj", ["payments"], verbose=False)

    out = " ".join(capsys.readouterr().out.split())
    assert "Maintenance mode enabled for 1 site(s)" in out


def test_migrate_batch_narration_verbose(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost", "b.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=True)

    out = " ".join(capsys.readouterr().out.split())
    assert "Migrating 2 affected site(s)" in out
    assert "Migration complete for all affected sites" in out


def test_migrate_batch_narration_when_nothing_to_migrate_verbose(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, [])  # no site has the app

    update_mod.run_app_update("proj", ["payments"], verbose=True)

    out = " ".join(capsys.readouterr().out.split())
    assert "No sites require migration" in out


def test_migrate_batch_narration_is_verbose_only(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost", "b.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=False)

    out = " ".join(capsys.readouterr().out.split())
    assert "Migrating 2 affected site(s)" not in out
    assert "Migration complete for all affected sites" not in out
    assert "No sites require migration" not in out


def test_build_intro_counts_only_apps_that_pulled_cleanly(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments", "hrms"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost"])
    container.fail_on = ["apps/hrms"]  # hrms' pull fails, so it is not buildable

    with pytest.raises(typer.Exit):
        update_mod.run_app_update("proj", ["payments", "hrms"], verbose=True, build=True)

    out = " ".join(capsys.readouterr().out.split())
    assert "Building assets for 1 app(s)" in out


def test_build_intro_is_verbose_only(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=False, build=True)

    out = " ".join(capsys.readouterr().out.split())
    assert "Building assets for" not in out


def test_no_recache_skip_message_is_verbose_only(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=True, no_recache=True)

    out = " ".join(capsys.readouterr().out.split())
    assert "Skipping recache (--no-recache flag set)" in out


def test_no_recache_skip_message_does_not_print_when_not_verbose(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=False, no_recache=True)

    out = " ".join(capsys.readouterr().out.split())
    assert "Skipping recache" not in out


def test_discover_narration_is_verbose_only_and_skips_failed_apps(monkeypatch, capsys):
    # A failed pull means the app is skipped BEFORE discovery, so it must produce
    # no discovery narration at all - not even "no sites found".
    container = FakeFrappeContainer(available_apps=["frappe", "payments", "hrms"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _patch_discovery_per_app(monkeypatch, {"payments": ["a.localhost"]})
    container.fail_on = ["apps/hrms"]  # hrms' pull fails -> skipped before discovery

    with pytest.raises(typer.Exit):
        update_mod.run_app_update("proj", ["payments", "hrms"], verbose=True)

    out = " ".join(capsys.readouterr().out.split())
    assert "Finding sites with 'payments' installed" in out
    assert "Found 1 site(s) with 'payments' installed" in out
    assert "Finding sites with 'hrms' installed" not in out


def test_discover_narration_does_not_print_when_not_verbose(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=False)

    out = " ".join(capsys.readouterr().out.split())
    assert "Finding sites with" not in out
    assert "Found 1 site(s)" not in out


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


# --------------------------------------------------- exec-stream error handling
#
# apps.py/update.py used to let a CwcliError from core.exec_stream (a lost
# connection, a failed exec start, an unknown exit code) escape as a raw
# traceback. These pin that it is now caught at the exec-loop choke point and
# rendered like run.py does, then exits non-zero.


class _BoomAPI:
    """A client.api whose exec_create always raises, like a dropped daemon connection."""

    def exec_create(self, *args, **kwargs):
        from docker.errors import DockerException

        raise DockerException("daemon gone")


def _boom_frappe_container():
    """A resolvable frappe container whose streaming exec surface is dead."""
    container = FakeFrappeContainer(available_apps=["frappe"])
    container.client = types.SimpleNamespace(api=_BoomAPI())
    return container


# These two REPLACE test_capture_bench_reports_cwclierror_cleanly and
# test_stream_bench_reports_cwclierror_cleanly, whose subjects (apps.py's
# _capture_bench / _stream_bench) moved into core/apps.py with the fan-out - the
# same move core.update made at the test below. The BEHAVIOUR they pinned is
# unchanged and still pinned here: a CwcliError out of exec_stream renders
# "Error:" to stderr and exits 1. It just cannot be asserted on a helper that no
# longer exists, so it is asserted where it now lives - through the command,
# which is a truer statement of it anyway.


def test_capture_path_reports_cwclierror_cleanly(wired, monkeypatch, capsys):
    """The drain-and-join path (list's per-site read) surfaces a lost stream."""
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: _boom_frappe_container())

    with pytest.raises(typer.Exit) as exc:
        apps_mod.list_apps(
            "proj",
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            installed=True,
            json_output=True,
            yes=False,
            verbose=False,
        )

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "Could not start the command" in err


def test_stream_path_reports_cwclierror_cleanly(wired, monkeypatch, capsys):
    """The render-each-event path (install's fan-out) surfaces a lost stream."""
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: _boom_frappe_container())

    with pytest.raises(typer.Exit) as exc:
        apps_mod.install_apps(
            "proj",
            ["custom_app"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            branch=None,
            fetch_only=False,
            json_output=False,
            yes=False,
            verbose=False,
        )

    assert exc.value.exit_code == 1
    assert "Error:" in capsys.readouterr().err


def test_update_reports_a_dead_daemon_as_unknown_and_still_exits_nonzero(monkeypatch, capsys):
    # REPLACES test_update_stream_command_reports_cwclierror_cleanly, whose subject
    # (update.py's _stream_command) moved into core.update with the state machine.
    #
    # The property it pinned - an exec-stream CwcliError is reported cleanly and
    # exits non-zero - survives, but its SHAPE changed by design (Decision 2): the
    # error no longer raises through as typer.Exit; it is recorded as UNKNOWN, the
    # fan-out continues, and the exit code comes from the report. Every exec-stream
    # error is unknown, INCLUDING exec.start_failed (this test's case): claiming "it
    # never ran" from an API call whose own outcome is uncertain would be a
    # confident guess, and the safe direction for a retry decision is unknown.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost"])
    container.client.api.exec_create = _BoomAPI().exec_create

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=True)

    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    out = " ".join(captured.out.split())
    assert "Could not start the command" in captured.err
    assert "Lost track of 1 app(s)" in out
    assert "payments: Git pull failed" not in out
