"""The one-exec batched read answers what the per-exec reads answer, on a real bench.

``core.bench_read`` replaces one ``bench --site X list-apps`` / ``bench execute
frappe.get_installed_apps`` boot per site with one Frappe process per bench, which
reproduces those commands' output through Frappe's own APIs. Whether that holds is
a property of each Frappe major's real code, so it is proven here on every version
leg, against the shared instance's genuine bench: the batched answers must equal
the per-exec answers exactly, the per-site ``bench`` commands included.

Read-only: nothing here writes the bench, a site, or the cache.
"""

from __future__ import annotations

import json

import docker
import pytest

from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import bench_read
from caffeinated_whale_cli.core import inspect as core_inspect

from . import harness

pytestmark = pytest.mark.e2e


def _noop(_event) -> None:
    pass


def _container(inst):
    container_id = harness.frappe_container_id(inst.name)
    assert container_id, f"no running frappe container for {inst.name}"
    return docker.from_env().containers.get(container_id)


def test_the_batched_full_read_equals_the_per_exec_read(running_instance):
    container = _container(running_instance)
    bench = running_instance.bench

    batch = bench_read.read_benches(container, [bench], list_apps=True, files=True)
    assert bench in batch.benches, f"no batched record (exit {batch.exit_code}): {batch.output}"
    batched = core_inspect._bench_dict(batch.benches[bench], _noop)
    per_exec = core_inspect._bench_dict(
        core_inspect._read_bench_per_exec(container, bench, _noop), _noop
    )

    # Positive first: a real read, not two equally empty answers.
    site = next(s for s in batched["sites"] if s["name"] == running_instance.site)
    assert site["installed_apps"][0].split()[0] == "frappe"
    assert "frappe" in batched["available_apps"]
    # Byte-identical, key order included (the human --json contract).
    assert json.dumps(batched) == json.dumps(per_exec)


def test_batched_installed_apps_equal_bench_execute(running_instance):
    container = _container(running_instance)
    bench = running_instance.bench
    sites = [running_instance.site, "cwe2e-no-such-site.localhost"]

    batched = core_apps._read_installed_apps(container, bench, sites, emit=_noop)
    per_exec = {site: core_apps._installed_apps(container, bench, site)[1:] for site in sites}

    ok, apps = batched[running_instance.site]
    assert ok is True and "frappe" in apps
    assert batched == per_exec
