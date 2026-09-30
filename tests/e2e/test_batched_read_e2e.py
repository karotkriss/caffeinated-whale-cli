"""The one-exec batched read answers what the ``bench`` commands answer, on a real bench.

``core.bench_read`` replaces one ``bench --site X list-apps`` / ``bench execute
frappe.get_installed_apps`` boot per site with one Frappe process per bench, which
reproduces those commands' output through Frappe's own APIs. Whether that holds is
a property of each Frappe major's real code, so it is proven here on every version
leg, against the shared instance's genuine bench: the batched answers must equal
the real per-site ``bench`` commands' output exactly.

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


def _bench(container, bench, site, *args):
    """A real ``bench --site <site> <args>`` in the bench: ``(exit code, output)``."""
    exit_code, output = container.exec_run(["bench", "--site", site, *args], workdir=bench)
    return exit_code, output.decode("utf-8", errors="replace")


def test_the_batched_full_read_equals_bench_list_apps(running_instance):
    container = _container(running_instance)
    bench = running_instance.bench

    batch = bench_read.read_benches(container, [bench], list_apps=True, files=True)
    assert bench in batch.benches, f"no batched record: {batch.errors}"
    batched = core_inspect._bench_dict(batch.benches[bench], _noop)

    # Positive first: a real read, not two equally empty answers.
    site = next(s for s in batched["sites"] if s["name"] == running_instance.site)
    assert site["installed_apps"][0].split()[0] == "frappe"
    assert "frappe" in batched["available_apps"]
    for site in batched["sites"]:
        exit_code, output = _bench(container, bench, site["name"], "list-apps")
        assert exit_code == 0, output
        assert site["installed_apps"] == bench_read.output_lines(output)


def test_batched_installed_apps_equal_bench_execute(running_instance):
    container = _container(running_instance)
    bench = running_instance.bench
    missing = "cwe2e-no-such-site.localhost"

    batched = core_apps._read_installed_apps(
        container, bench, [running_instance.site, missing], emit=_noop
    )

    exit_code, output = _bench(
        container, bench, running_instance.site, "execute", "frappe.get_installed_apps"
    )
    assert exit_code == 0, output
    lines = [line for line in output.splitlines() if line.strip()]
    apps, cause = batched[running_instance.site]
    assert cause is None and "frappe" in apps
    assert apps == json.loads(lines[-1])
    apps, cause = batched[missing]
    assert apps is None
    assert cause is not None and cause.startswith(f"{bench}: ")
