"""``core.apps`` - the branches the CLI frontend cannot reach.

`tests/test_apps.py` + `tests/test_apps_characterization.py` drive the three verbs
through `commands/apps.py`, which PRE-RESOLVES the bench and starts the container
itself (the `backup`/`update` frontend pattern). That is the right shape for the
CLI, but it means the core's own forks - a stopped container, an ambiguous
multi-bench project, the no-cache default fallback, and the destructive-consent
gate - are never exercised from there. They are what `axi` and a future GUI hit,
so they are pinned here.

Also pinned: the purity properties the whole rework rests on - the core prints
NOTHING, returns no live Docker object, and `dataclasses.asdict` on what it
returns is plain serializable data.
"""

from __future__ import annotations

import dataclasses

import pytest

from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/frappe-bench"


class FakeAPI:
    """Streaming exec surface (client.api), mirroring docker-py's shape."""

    def __init__(self, container):
        self.container = container
        self._pending = None

    def exec_create(self, cid, cmd, workdir=None, tty=False, environment=None):
        self._pending = (cmd, workdir)
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=True, demux=False):
        cmd, _workdir = self._pending
        code, out = self.container._run(cmd)
        self.container._last_code = code
        raw = out.encode() if isinstance(out, str) else out
        yield (raw, None) if demux else raw

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.container._last_code}


class FakeContainer:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    id = "cid"

    def __init__(self, *, status="running", available=("frappe",), installed=None, fail_on=()):
        self.status = status
        self.available = list(available)
        self.installed = installed or {}
        self.fail_on = list(fail_on)
        self.calls: list[str] = []
        self._last_code = 0
        import types

        self.client = types.SimpleNamespace(api=FakeAPI(self))

    def reload(self):
        pass

    def _run(self, cmd):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(cmd_str)
        for sub in self.fail_on:
            if sub in cmd_str:
                return 1, f"boom: {cmd_str}"
        if cmd_str.startswith("ls -1") and cmd_str.rstrip().endswith("apps"):
            return 0, "\n".join(self.available) + "\n"
        if "list-apps" in cmd_str:
            import shlex

            parts = shlex.split(cmd_str)
            site = parts[parts.index("--site") + 1] if "--site" in parts else ""
            return 0, "\n".join(self.installed.get(site, [])) + "\n"
        return 0, ""

    def exec_run(self, cmd, workdir=None, **kwargs):
        code, out = self._run(cmd)
        return code, out.encode() if isinstance(out, str) else out


@pytest.fixture()
def container(monkeypatch):
    c = FakeContainer(installed={"a.localhost": ["frappe 15.0.0 version-15"]})
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(core_apps.bench_sites, "list_sites", lambda *a, **k: ["a.localhost"])
    return c


def _cache(monkeypatch, benches):
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: benches)


# ------------------------------------------------------------------ the shared forks


def test_a_stopped_container_is_a_returned_choice_never_a_start(monkeypatch, container):
    """The core REPORTS the fork; performing the start is the frontend's job."""
    container.status = "exited"
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.list_apps("proj")

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_start"
    assert container.calls == []  # nothing ran against a stopped container


def test_multibench_with_no_selector_is_a_returned_choice(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}, {"path": "/workspace/other-bench"}])

    result = core_apps.list_apps("proj")

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "select_bench"
    assert result.choice.param == "bench"
    assert len(result.choice.options) == 2


def test_no_cached_bench_falls_back_to_the_historical_default(monkeypatch, container):
    """Matches `run` and the pre-migration `_resolve_bench`'s `or _DEFAULT_BENCH`."""
    _cache(monkeypatch, [])

    result = core_apps.list_apps("proj")

    assert result.data.bench_path == resolvers.DEFAULT_BENCH_PATH
    assert any(w.code == "bench.default_used" for w in result.warnings)


def test_a_missing_project_raises_rather_than_returning(monkeypatch):
    def _boom(_p):
        raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'nope' not found.")

    monkeypatch.setattr(core_docker, "get_frappe_container", _boom)

    with pytest.raises(CwcliError) as exc:
        core_apps.list_apps("nope")
    assert exc.value.kind is ErrorKind.NOT_FOUND


# ------------------------------------------------------------------------ list_apps


def test_list_keeps_only_the_app_name_from_a_real_list_apps_line(monkeypatch, container):
    """A real bench prints `<name> <version> <branch>`; only the name is an app."""
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.list_apps("proj", installed=True)

    assert result.data.installed == {"a.localhost": ["frappe"]}
    assert result.status is Status.OK
    assert result.data.ok is True


def test_list_reports_a_failed_site_read_as_none_not_empty(monkeypatch, container):
    """ "no apps" and "could not tell" are different facts, and only one exits 1."""
    container.fail_on = ["list-apps"]
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.list_apps("proj", installed=True)

    assert result.data.installed == {"a.localhost": None}
    assert result.data.ok is False
    assert result.status is Status.WARNING


def test_list_does_not_read_sites_unless_asked(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.list_apps("proj")

    assert result.data.installed == {}
    assert not any("list-apps" in c for c in container.calls)


# --------------------------------------------------------------------- install_apps


def test_install_fans_out_over_every_site_and_reports_each(monkeypatch, container):
    monkeypatch.setattr(
        core_apps.bench_sites, "list_sites", lambda *a, **k: ["b.localhost", "a.localhost"]
    )
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"])

    assert result.data.ok is True
    # Sorted, so the fan-out order is deterministic rather than filesystem order.
    assert [(r.action, r.site) for r in result.data.results] == [
        ("get-app", None),
        ("install-app", "a.localhost"),
        ("install-app", "b.localhost"),
    ]


def test_install_partial_failure_is_a_warning_envelope_carrying_ok_false(monkeypatch, container):
    """The trap: WARNING maps to exit 0 everywhere else, so the frontend reads .ok."""
    container.fail_on = ["--site b.localhost install-app"]
    monkeypatch.setattr(
        core_apps.bench_sites, "list_sites", lambda *a, **k: ["a.localhost", "b.localhost"]
    )
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"])

    assert result.status is Status.WARNING
    assert result.data.ok is False
    assert [r.ok for r in result.data.results] == [True, True, False]


def test_install_fetch_only_never_touches_a_site(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"], fetch_only=True)

    assert not any("install-app" in c for c in container.calls)
    assert [r.action for r in result.data.results] == ["get-app"]


def test_install_shell_interpolations_are_shlex_quoted(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    core_apps.install_apps("proj", ["a; rm -rf /"], branch="x; whoami", sites=["s; echo"])

    fetch = next(c for c in container.calls if c.startswith("bench get-app"))
    assert "'a; rm -rf /'" in fetch
    assert "'x; whoami'" in fetch


# ------------------------------------------------------------------- uninstall_apps


def test_uninstall_without_consent_is_a_choice_and_runs_nothing(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.uninstall_apps("proj", ["payments"])

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_uninstall"
    assert result.choice.param == "consent"
    assert "a.localhost" in result.choice.prompt
    assert not any("uninstall-app" in c for c in container.calls)


def test_uninstall_with_consent_fans_out(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.uninstall_apps("proj", ["payments"], consent=True)

    assert result.data.ok is True
    assert any("uninstall-app payments --yes" in c for c in container.calls)


def test_uninstall_with_no_sites_is_a_clean_noop_before_the_gate(monkeypatch, container):
    """No sites is decided BEFORE consent: there is nothing to consent to."""
    monkeypatch.setattr(core_apps.bench_sites, "list_sites", lambda *a, **k: [])
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.uninstall_apps("proj", ["payments"])

    assert result.status is Status.OK
    assert result.data.results == []
    assert any(w.code == "sites.none" for w in result.warnings)


def test_uninstall_consent_is_independent_of_auto_start(monkeypatch, container):
    """The CLI fuses them behind --yes; the core must not.

    auto_start=True must NOT imply destructive consent - that fusion is a UX
    choice of one frontend, and a core that serves axi and a GUI models the two
    as what they are.
    """
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.uninstall_apps("proj", ["payments"], auto_start=True)

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_uninstall"


# ---------------------------------------------------------------------- the contract


def test_the_core_prints_nothing_at_all(monkeypatch, container, capsys):
    _cache(monkeypatch, [{"path": BENCH}])

    core_apps.list_apps("proj", installed=True)
    core_apps.install_apps("proj", ["payments"])
    core_apps.uninstall_apps("proj", ["payments"], consent=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("verb", ["listing", "report"])
def test_what_the_core_returns_is_plain_serializable_data(monkeypatch, container, verb):
    """No live Docker object may cross a core.<verb> return boundary."""
    _cache(monkeypatch, [{"path": BENCH}])

    if verb == "listing":
        data = core_apps.list_apps("proj", installed=True).data
    else:
        data = core_apps.install_apps("proj", ["payments"]).data

    plain = dataclasses.asdict(data)
    assert isinstance(plain, dict)

    def _plain(value):
        if isinstance(value, dict):
            return all(_plain(v) for v in value.values())
        if isinstance(value, list):
            return all(_plain(v) for v in value)
        return value is None or isinstance(value, (str, int, float, bool))

    assert _plain(plain), plain
