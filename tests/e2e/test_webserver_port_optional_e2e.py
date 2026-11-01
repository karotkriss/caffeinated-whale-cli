"""Real-Docker E2E: cwcli works whether or not a bench sets ``webserver_port``.

An administrator removes ``webserver_port`` from ``sites/common_site_config.json``
on a bench served by its hostname through a reverse proxy: it is the only way to
stop Frappe appending the internal port to the URLs it builds (``get_url``, and
every SSO/OAuth redirect URL made from it), and the bench runs fully without it.
cwcli used to treat the missing key as "port unknown", so Open site resolved no
address, ``status`` stopped probing the web server, and ``start`` skipped its wait.

These tests remove the key the way an administrator does (bench's own ``config
remove-common-config``, through ``cwcli run``) on the shared session instance, and
put the bench's original config back in a ``finally`` - the shared-group rule.
The warning for a hostname address that still carries the port is unit-tested
(``tests/test_core_url.py``, ``tests/test_webui_start_gate.py``): proving it here
would need a second, non-localhost site on the shared bench.
"""

from __future__ import annotations

import json
import shlex
from contextlib import contextmanager

import pytest

from . import harness
from .test_axi_url_e2e import _docker_port, _parse_flat_fields
from .test_start_status_e2e import _ensure_serving, _wait_supervised_stack

pytestmark = [pytest.mark.e2e]


def _common_config(inst) -> dict:
    code, out = harness.exec_in_frappe(inst.name, f"cat {inst.bench}/sites/common_site_config.json")
    assert code == 0, out
    return json.loads(out)


@contextmanager
def _without_webserver_port(inst):
    """Remove the key with bench's own command; restore the exact original file."""
    original = _common_config(inst)
    assert "webserver_port" in original, "the session bench must start with webserver_port"
    try:
        res = harness.run_cwcli(
            "run", inst.name, "config", "remove-common-config", "webserver_port"
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "webserver_port" not in _common_config(inst)
        yield
    finally:
        payload = shlex.quote(json.dumps(original, indent=1))
        harness.exec_in_frappe(
            inst.name, f"echo {payload} > {inst.bench}/sites/common_site_config.json"
        )


@contextmanager
def _host_name(inst, value: str):
    """Set the site's ``host_name`` with Frappe's own ``set-config``; remove it after."""
    try:
        res = harness.run_cwcli(
            "run", inst.name, "--site", inst.site, "set-config", "host_name", value
        )
        assert res.returncode == 0, res.stdout + res.stderr
        yield
    finally:
        harness.run_cwcli("run", inst.name, "--site", inst.site, "set-config", "host_name", "None")


def _axi_url(inst) -> tuple[dict[str, str], str]:
    res = harness.run_cwcli("axi", "url", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    out = harness.strip_ansi(res.stdout)
    return _parse_flat_fields(out), out


def test_open_site_status_and_web_restart_work_without_webserver_port(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    _wait_supervised_stack(inst.name)
    published = _docker_port(harness.frappe_container_id(inst.name), 8000)

    with _without_webserver_port(inst):
        # Open site: the localhost site keeps its published port, and the bench is
        # probed on the port its Procfile's web line binds.
        fields, out = _axi_url(inst)
        assert fields["url"] == f"http://{inst.site}:{published}", out
        assert fields["reachable"] == "true", out
        assert fields["http_code"] == "200", out
        assert fields["internal_port"] == "false", out
        assert "url.unresolved" not in out and "Could not determine" not in out, out

        # Health: the web port is known, and the web server was actually probed.
        status = harness.run_cwcli("axi", "status", inst.name)
        assert status.returncode == 0, status.stdout + status.stderr
        status_out = harness.strip_ansi(status.stdout)
        assert "web_port: 8000" in status_out, status_out
        assert "web_port_verified: true" in status_out, status_out
        assert 'web_http_code: "200"' in status_out, status_out

        # Restarting the web process works and the site answers again afterwards.
        restart = harness.run_cwcli("axi", "restart", inst.name, "--process", "web")
        assert restart.returncode == 0, restart.stdout + restart.stderr
        assert "supervisor_state: RUNNING" in harness.strip_ansi(restart.stdout)
        harness.wait_until(
            lambda: _axi_url(inst)[0].get("http_code") == "200",
            timeout=120,
            interval=3,
            desc="the site to answer after the web restart",
        )

    _wait_supervised_stack(inst.name)


def test_the_sites_host_name_is_its_address(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    port = _common_config(inst)["webserver_port"]
    published = _docker_port(harness.frappe_container_id(inst.name), int(port))

    with _host_name(inst, "https://site.example.com"):
        # Frappe's get_url appends webserver_port to a host_name that names no port,
        # so the address keeps it and is flagged, with the steps that remove the key.
        fields, out = _axi_url(inst)
        assert fields["url"] == f"https://site.example.com:{port}", out
        assert fields["source"] == "host_name", out
        assert fields["internal_port"] == "true", out
        assert "remove-common-config webserver_port" in out, out
        # The bench itself is still probed on its own port, for the site's Host.
        assert fields["http_code"] == "200", out

        with _without_webserver_port(inst):
            fields, out = _axi_url(inst)
            assert fields["url"] == "https://site.example.com", out
            assert fields["internal_port"] == "false", out

    # A localhost host_name opens on the port Docker publishes, never the
    # container port it names.
    with _host_name(inst, f"http://{inst.site}:{port}"):
        fields, out = _axi_url(inst)
        assert fields["url"] == f"http://{inst.site}:{published}", out
        assert fields["source"] == "host_name", out
        assert fields["internal_port"] == "false", out

    fields, out = _axi_url(inst)
    assert fields["source"] == "site", out
