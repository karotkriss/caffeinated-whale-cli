"""``core.bench_read``'s one-exec full read, run for real on the host.

The unit fakes answer the batched read from per-path answers
(``tests/batched_probes.py``), so these tests pin what the fakes cannot: the real
``sh`` loop and the real Python process, against a real bench tree under
``tmp_path`` with a stand-in ``frappe`` package and a stand-in ``bench`` CLI.

The ``bench`` stand-in answers ``--site X list-apps`` with the body of Frappe's
own ``frappe.commands.site.list_apps`` (text format), so the characterization test
compares the batched read against the ``bench`` commands over the same data.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

import pytest

from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import bench_read
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import config_utils

FAKE_FRAPPE = """\
import json, os

if os.path.exists(os.path.join(os.path.dirname(__file__), "BROKEN")):
    raise ImportError("frappe is broken")

if os.path.exists(os.path.join(os.path.dirname(__file__), "NOISY")):
    print("frappe chatter on stdout")

local = {}


def init(site=None, sites_path="."):
    if local.get("initialised"):
        return  # Frappe's own guard: without destroy() the next site is never switched to
    if not os.path.isfile(os.path.join(sites_path, site, "site_config.json")):
        raise RuntimeError(site + " does not exist")
    local.update(site=site, initialised=True)


def connect():
    with open(os.path.join(local["site"], "fake_db.json")) as handle:
        data = json.load(handle)
    if data.get("down"):
        raise RuntimeError("cannot reach database " + data["db_name"])
    local["db"] = data


class _Row(dict):
    __getattr__ = dict.__getitem__


def get_single(doctype):
    assert doctype == "Installed Applications"
    return _Row(installed_applications=[_Row(row) for row in local["db"].get("rows", [])])


def get_installed_apps():
    return list(local["db"].get("installed", []))


def destroy():
    local.clear()
"""

# The per-exec path's `bench` CLI: Frappe v15's list_apps text body for one site,
# and `execute` printing the JSON of a truthy return, as bench does.
FAKE_BENCH = """\
import json, os, sys

args = sys.argv[1:]
site = args[args.index("--site") + 1]
command = args[args.index("--site") + 2 :]
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "frappe"))
os.chdir("sites")
import frappe

frappe.init(site=site)
frappe.connect()
if command == ["list-apps"]:
    apps = frappe.get_single("Installed Applications").installed_applications

    def format_app(app):
        name_len = max(len(app.app_name) for app in apps)
        ver_len = max(len(app.app_version) for app in apps)
        template = f"{{0:{name_len}}} {{1:{ver_len}}} {{2}}"
        return template.format(app.app_name, app.app_version, app.git_branch)

    info = [format_app(app) for app in apps] if apps else frappe.get_installed_apps()
    info_str = "\\n".join(info)
    summary = f"\\n{info_str}\\n"
    if info and summary:
        print(summary)
elif command == ["execute", "frappe.get_installed_apps"]:
    ret = frappe.get_installed_apps()
    if ret:
        print(json.dumps(ret))
frappe.destroy()
"""

ROWS = [
    {"app_name": "frappe", "app_version": "15.40.0", "git_branch": "version-15"},
    {"app_name": "erpnext", "app_version": "15.3.1", "git_branch": "version-15"},
]


class HostContainer:
    """Runs execs on the host the way docker-py does: a string command is
    shlex-split (no shell), ``workdir`` is the cwd, stderr merges into stdout."""

    def __init__(self, bin_dir):
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        self.calls: list = []

    def exec_run(self, cmd, workdir=None, environment=None):
        self.calls.append(cmd)
        argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
        try:
            proc = subprocess.run(
                argv,
                cwd=workdir,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
        except OSError as error:  # docker reports an unrunnable command as exit 126/127
            return 127, str(error).encode()
        return proc.returncode, proc.stdout


def make_bench(root, *, venv=True, noisy=False, broken=False, label="primary"):
    """A bench with three sites: readable, database down, and no app rows."""
    bench = root / "frappe-bench"
    (bench / "apps" / "frappe" / "frappe").mkdir(parents=True)
    (bench / "apps" / "frappe" / "frappe" / "__init__.py").write_text(FAKE_FRAPPE)
    if noisy:
        (bench / "apps" / "frappe" / "frappe" / "NOISY").write_text("")
    if broken:
        (bench / "apps" / "frappe" / "frappe" / "BROKEN").write_text("")
    (bench / "apps" / "erpnext").mkdir()
    if venv:
        python = bench / "env" / "bin" / "python"
        python.parent.mkdir(parents=True)
        pythonpath = shlex.quote(str(bench / "apps" / "frappe"))
        python.write_text(
            f'#!/bin/sh\nPYTHONPATH={pythonpath} exec {shlex.quote(sys.executable)} "$@"\n'
        )
        python.chmod(0o755)
    sites = bench / "sites"
    sites.mkdir()
    (sites / "common_site_config.json").write_text(json.dumps({"default_site": "a.localhost"}))
    (sites / "currentsite.txt").write_text("a.localhost\n")
    (sites / "apps.txt").write_text("frappe\nerpnext\n")
    (sites / "assets").mkdir()
    site_dbs = {
        "a.localhost": {"rows": ROWS, "installed": ["frappe", "erpnext"]},
        "b.localhost": {"down": True, "db_name": "_secretdb"},
        "c site's.localhost": {"rows": [], "installed": ["frappe"]},
    }
    for name, db in site_dbs.items():
        (sites / name).mkdir()
        (sites / name / "site_config.json").write_text(json.dumps({"db_name": name[:1]}))
        (sites / name / "fake_db.json").write_text(json.dumps(db))
    (bench / ".cwcli").mkdir()
    (bench / ".cwcli" / ".bench-label").write_text(json.dumps({"schema": 1, "label": label}))
    return str(bench)


@pytest.fixture()
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_bench = bin_dir / "bench"
    fake_bench.write_text(f"#!{sys.executable}\n{FAKE_BENCH}")
    fake_bench.chmod(0o755)
    return HostContainer(bin_dir)


# A search root no shell could take unquoted: spaces, both quotes, and metacharacters.
HOSTILE_ROOT = "root with space; $(touch pwned) `x` 'q' \"d\" & |"


def _dict(read):
    events: list = []
    return core_inspect._bench_dict(read, events.append), [
        e.text for e in events if isinstance(e, core_inspect.InspectWarning)
    ]


def _bench_list_apps(host, bench, site):
    """``bench --site X list-apps`` lines, or [] when the command failed."""
    code, out = host.exec_run(f"bench --site {shlex.quote(site)} list-apps", workdir=bench)
    return bench_read.output_lines(out.decode()) if code == 0 else []


@pytest.mark.parametrize("root_name", ["plain", HOSTILE_ROOT], ids=["plain", "hostile"])
def test_the_batched_read_equals_the_bench_commands(tmp_path, host, root_name):
    root = tmp_path / root_name
    bench = make_bench(root)

    batch = bench_read.read_benches(host, [bench], list_apps=True, files=True)
    bench_dict, warnings = _dict(batch.benches[bench])

    assert list(bench_dict) == [
        "path",
        "sites",
        "available_apps",
        "current_site",
        "label",
        "common_site_config",
    ]
    assert bench_dict["available_apps"] == sorted(os.listdir(os.path.join(bench, "apps")))
    assert [s["name"] for s in bench_dict["sites"]] == [
        "a.localhost",
        "b.localhost",
        "c site's.localhost",
    ]
    for site in bench_dict["sites"]:
        assert list(site) == ["name", "installed_apps", "site_config"]
        assert site["installed_apps"] == _bench_list_apps(host, bench, site["name"])
    assert bench_dict["sites"][0]["installed_apps"] == [
        "frappe  15.40.0 version-15",
        "erpnext 15.3.1  version-15",
    ]
    assert bench_dict["sites"][1]["installed_apps"] == []
    assert bench_dict["sites"][2]["installed_apps"] == ["frappe"]
    # The failed site names its bench and step; the exception's message, which here
    # quotes the site's db_name, never rides along.
    assert warnings == [
        f"Failed to list apps for site 'b.localhost' "
        f"({bench}: frappe.connect failed (RuntimeError))."
    ]
    assert "_secretdb" not in json.dumps(warnings)
    assert bench_dict["current_site"] == "a.localhost"
    assert bench_dict["label"] == "primary"
    assert bench_dict["common_site_config"] == {"default_site": "a.localhost"}
    assert not (tmp_path / "pwned").exists()
    assert not (root / "pwned").exists()


def test_one_exec_and_one_process_read_every_bench(tmp_path, host):
    benches = [make_bench(tmp_path / "one"), make_bench(tmp_path / "two", label="second")]

    batch = bench_read.read_benches(host, benches, list_apps=True, files=True)

    assert len(host.calls) == 1
    assert set(batch.benches) == set(benches)
    assert batch.benches[benches[1]].label == "second"


def test_a_hostile_search_root_caches_real_apps_and_a_cache_hit_sees_no_drift(
    tmp_path, host, monkeypatch
):
    """The deferred fix-1-3 finding: an unquoted full read cached [] apps for such a
    bench, so every later cache-hit refresh saw drift and re-ran the full read."""
    root = tmp_path / HOSTILE_ROOT
    bench = make_bench(root)
    monkeypatch.setattr(
        config_utils,
        "load_config",
        lambda: {"search_paths": {"custom_bench_paths": [str(root)]}},
    )

    assert bench in core_inspect.discover_benches(host)
    gathered = core_inspect._gather_benches(host, [bench], lambda _e: None)
    assert gathered[0]["available_apps"] == ["erpnext", "frappe"]
    assert len(gathered[0]["sites"]) == 3

    _refreshed, drift = core_inspect.partial_refresh(host, gathered)
    assert drift is False


def test_frappe_chatter_on_stdout_never_reaches_the_record(tmp_path, host):
    bench = make_bench(tmp_path / "b", noisy=True)

    read = bench_read.read_benches(host, [bench], list_apps=True).benches[bench]

    assert read.sites is not None
    assert [s.list_apps for s in read.sites] == [
        ["frappe  15.40.0 version-15", "erpnext 15.3.1  version-15"],
        None,
        ["frappe"],
    ]


def test_without_a_venv_the_files_are_read_but_frappe_is_never_asked(tmp_path, host):
    bench = make_bench(tmp_path / "b", venv=False)

    read = bench_read.read_benches(host, [bench], list_apps=True, files=True).benches[bench]

    assert read.available_apps == ["erpnext", "frappe"]
    assert read.label == "primary"
    assert all(not imp.checked for imp in read.app_imports.values())
    assert read.sites is not None
    assert [s.list_apps for s in read.sites] == [None, None, None]
    assert {s.error for s in read.sites} == {
        f"no bench virtualenv python at {bench}/env/bin/python"
    }


def test_a_failed_frappe_import_names_the_bench_and_step_for_every_site(tmp_path, host):
    bench = make_bench(tmp_path / "b", broken=True)

    batch = bench_read.read_benches(host, [bench], list_apps=True, files=True)
    bench_dict, warnings = _dict(batch.benches[bench])

    assert [s["installed_apps"] for s in bench_dict["sites"]] == [[], [], []]
    assert warnings == [
        f"Failed to list apps for site '{name}' ({bench}: import frappe failed (ImportError))."
        for name in ["a.localhost", "b.localhost", "c site's.localhost"]
    ]


def test_a_bench_whose_process_dies_is_named_with_its_cause_and_never_cached(tmp_path, host):
    bench = make_bench(tmp_path / "b")
    healthy = make_bench(tmp_path / "healthy")
    python = tmp_path / "b" / "frappe-bench" / "env" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 3\n")

    batch = bench_read.read_benches(host, [bench, healthy], list_apps=True, files=True)

    assert set(batch.benches) == {healthy}
    assert batch.errors == {bench: "its read process exited with code 3"}
    host.calls.clear()
    with pytest.raises(CwcliError) as exc:
        core_inspect._gather_benches(host, [bench, healthy], lambda _e: None)
    assert (exc.value.kind, exc.value.code) == (ErrorKind.DOCKER, "inspect.read_failed")
    assert f"{bench} (its read process exited with code 3)" in exc.value.message
    assert len(host.calls) == 1  # no second, per-exec path


def _bench_execute(host, bench, site):
    """The apps ``bench execute frappe.get_installed_apps`` prints, or None."""
    cmd = f"bench --site {shlex.quote(site)} execute frappe.get_installed_apps"
    code, out = host.exec_run(cmd, workdir=bench)
    lines = [line for line in out.decode().splitlines() if line.strip()]
    return json.loads(lines[-1]) if code == 0 and lines else None


def test_installed_apps_match_bench_execute_for_every_site(tmp_path, host):
    bench = make_bench(tmp_path / HOSTILE_ROOT)
    sites = ["a.localhost", "b.localhost", "c site's.localhost", "missing.localhost"]

    batched = core_apps._read_installed_apps(host, bench, sites, emit=lambda _e: None)

    assert {site: apps for site, (apps, _cause) in batched.items()} == {
        site: _bench_execute(host, bench, site) for site in sites
    }
    assert batched["a.localhost"] == (["frappe", "erpnext"], None)
    assert batched["b.localhost"] == (None, f"{bench}: frappe.connect failed (RuntimeError)")
    assert batched["missing.localhost"] == (None, f"{bench}: frappe.init failed (RuntimeError)")
