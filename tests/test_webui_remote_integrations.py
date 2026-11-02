"""The hub's Settings > Integrations host picker and the host-aware publish flow, as the
web UI's data modules decide them, executed under node: a remote host's integrations
are read, tested and saved through the hub's forward of that host (never this
machine's store), community installs stay local, an Operator-capped host gets the
Admin-cap message naming the exact grant command, every link state is named, and a
publish on a remote instance reads that host's own store and says whose settings it
uses (webui/src/data/integrations.js, hosts.js, publish.js).

The real modules are imported; only their browser-side imports (vue, frappe-ui, the
session store and the API transport) are stubbed, the way tests/test_webui_hosts.py
loads the page's data modules. The transport stub records every request's method and
URL, which is what proves where each call goes."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "webui" / "src" / "data"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUBS = {
    "vue": "export const reactive = (o) => o\nexport const computed = (fn) => ({ get value() { return fn() } })",
    "frappe-ui": (
        "const record = (kind) => (title, options) => globalThis.__t.toasts.push({ kind, title, options })\n"
        "export const toast = { error: record('error'), success: record('success'), warning: record('warning') }"
    ),
    "./session": "export const can = () => true\nexport const refuseBelow = () => false\nexport const session = {}",
    "./api": (
        "export const apiBase = (host) => (host ? `/api/v1/hosts/${encodeURIComponent(host)}` : '/api/v1')\n"
        "globalThis.__t = { calls: [], answers: {}, toasts: [] }\n"
        "const answer = (url) => { const a = globalThis.__t.answers[url]; if (a instanceof Error) throw a; return a ?? {} }\n"
        "export const getJson = async (url) => { globalThis.__t.calls.push(['GET', url]); return answer(url) }\n"
        "export const postJson = async (url, payload) => { globalThis.__t.calls.push(['POST', url, payload ?? null]); return answer(url) }"
    ),
}

HOOKS = """
const STUBS = %s
export async function resolve(specifier, context, next) {
  if (specifier in STUBS) return { url: `stub:${specifier}`, shortCircuit: true }
  if (specifier.startsWith('.') && !/\\.\\w+$/.test(specifier)) specifier += '.js'
  return next(specifier, context)
}
export async function load(url, context, next) {
  if (url.startsWith('stub:')) return { format: 'module', source: STUBS[url.slice(5)], shortCircuit: true }
  return next(url, context)
}
"""


@pytest.fixture
def node(tmp_path):
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(HOOKS % json.dumps(STUBS))
    register = tmp_path / "register.mjs"
    register.write_text(
        f"import {{ register }} from 'node:module'\nregister({json.dumps(hooks.as_uri())})\n"
    )

    def _run(body: str):
        script = (
            f"const out = await (async () => {{ {body} }})()\n"
            "console.log(JSON.stringify(out ?? null))\n"
        )
        done = subprocess.run(
            ["node", "--import", register.as_uri(), "--input-type=module"],
            input=script,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(done.stdout)

    return _run


def _mod(name: str) -> str:
    return f"await import({json.dumps((DATA / name).as_uri())})"


HUB = {
    "name": "hub.example.com",
    "allow_command": 'cwcli remote allow --hub hub.example.com --key "ssh-ed25519 AAAA cwcli-hub:hub.example.com" --max-role operator',
    "allow_admin_command": 'cwcli remote allow --hub hub.example.com --key "ssh-ed25519 AAAA cwcli-hub:hub.example.com" --max-role admin',
}


def _host(**over):
    return {
        "id": "remote",
        "name": "remote.example.com",
        "state": "connected",
        "cap": "operator",
        "reason": None,
        **over,
    }


class TestTheAdminCapMessage:
    def _refusal(self, node, host):
        return node(
            f"const {{ integrationsRefusal }} = {_mod('hosts.js')}\n"
            f"return integrationsRefusal({json.dumps(host)}, {json.dumps(HUB)})"
        )

    def test_an_operator_capped_host_needs_the_admin_cap_and_the_exact_grant_command_is_named(
        self, node
    ):
        got = self._refusal(node, _host())
        assert got["kind"] == "cap"
        assert got["title"] == "Configuring remote.example.com's integrations needs the Admin cap"
        assert "granted this hub the operator role" in got["text"]
        assert "On remote.example.com, as the user who runs cwcli there" in got["text"]
        # The command is the hub's own, at the Admin cap: never typed by hand here.
        assert got["command"] == HUB["allow_admin_command"]
        assert got["command"].endswith("--max-role admin")

    def test_a_viewer_cap_gets_the_same_message_without_claiming_it_can_run_the_fleet(self, node):
        got = self._refusal(node, _host(cap="viewer"))
        assert got["kind"] == "cap" and "the viewer role" in got["text"]
        assert "can only read the fleet there" in got["text"]
        assert "can run the fleet" not in got["text"]
        assert got["command"] == HUB["allow_admin_command"]

    def test_an_admin_capped_connected_host_is_not_refused(self, node):
        assert self._refusal(node, _host(cap="admin")) is None

    @pytest.mark.parametrize(
        ("state", "cap", "words"),
        [
            ("unreachable", "admin", "stopped answering"),
            ("host_key_changed", "admin", "different SSH host key"),
            ("auth_refused", None, "no longer accepts this hub's key"),
            ("daemon_down", "admin", "cwcli is not running"),
            ("version_mismatch", "admin", "actions wait"),
            ("connecting", None, "Connecting now"),
        ],
    )
    def test_every_other_link_state_is_named_before_the_cap_even_at_the_admin_cap(
        self, node, state, cap, words
    ):
        got = self._refusal(node, _host(state=state, cap=cap))
        assert got["kind"] == "link" and got["command"] is None
        assert got["title"].startswith("remote.example.com is ") and words in got["text"]

    def test_a_missing_hub_record_leaves_the_message_without_a_command(self, node):
        got = node(
            f"const {{ integrationsRefusal }} = {_mod('hosts.js')}\n"
            f"return integrationsRefusal({json.dumps(_host())}, null)"
        )
        assert got["kind"] == "cap" and got["command"] is None


class TestARemotesIntegrationsGoThroughItsForward:
    def test_the_picked_host_routes_the_list_tests_saves_and_actions_but_never_community_installs(
        self, node
    ):
        calls = node(
            f"const m = {_mod('integrations.js')}\n"
            "globalThis.__t.answers['/api/v1/hosts/remote/integrations'] = { integrations: [{ id: 'npm' }], check: { ok: true }, integration: { enabled: true } }\n"
            "globalThis.__t.answers['/api/v1/integrations'] = { integrations: [{ id: 'local' }] }\n"
            "await m.loadIntegrations()\n"
            "const localList = m.integrations.list.map((i) => i.id)\n"
            "m.pickIntegrationsHost('remote')\n"
            "const cleared = m.integrations.list.length\n"
            "await m.loadIntegrations()\n"
            "const remoteList = m.integrations.list.map((i) => i.id)\n"
            "await m.testIntegration('npm', { url: 'http://127.0.0.1:81' }, { password: 'x' })\n"
            "await m.configureIntegration('npm', { url: 'u' }, {}, true)\n"
            "await m.integrationAction('npm', 'detect', {}, {}, {})\n"
            "await m.disableIntegration('npm')\n"
            "await m.addCommunity('https://example.com/r.git', 'main', 'abc')\n"
            "await m.updateCommunity('x', 'abc')\n"
            "m.pickIntegrationsHost(null)\n"
            "await m.loadIntegrations()\n"
            "return { localList, cleared, remoteList, calls: globalThis.__t.calls, host: m.integrations.host }"
        )
        assert calls["localList"] == ["local"] and calls["cleared"] == 0
        assert calls["remoteList"] == ["npm"] and calls["host"] is None
        remote = "/api/v1/hosts/remote/integrations"
        # Turning one on or off reads that host's publications again, so Publish follows it.
        pubs = "/api/v1/hosts/remote/publications"
        assert [c[:2] for c in calls["calls"]] == [
            ["GET", "/api/v1/integrations"],
            ["GET", remote],
            ["POST", remote],
            ["POST", remote],
            ["GET", pubs],
            ["POST", remote],
            ["POST", remote],
            ["GET", pubs],
            ["POST", "/api/v1/integrations"],
            ["POST", "/api/v1/integrations"],
            ["GET", "/api/v1/integrations"],
        ]
        ops = [c[2]["op"] for c in calls["calls"] if c[0] == "POST"]
        assert ops == [
            "test",
            "configure",
            "action",
            "disable",
            "community_add",
            "community_update",
        ]
        # Detect is an action, so it runs on the picked host: its container discovery, not the hub's.
        assert calls["calls"][5][2] == {
            "op": "action",
            "id": "npm",
            "action": "detect",
            "args": {},
            "settings": {},
            "secrets": {},
        }

    def test_a_read_that_fails_is_reported_for_that_host_only(self, node):
        got = node(
            f"const m = {_mod('integrations.js')}\n"
            "globalThis.__t.answers['/api/v1/hosts/remote/integrations'] = Object.assign(new Error('Your role (Operator) cannot do this; it needs Admin.'), { status: 403, code: 'auth.role_forbidden' })\n"
            "m.pickIntegrationsHost('remote')\n"
            "await m.loadIntegrations()\n"
            "const remoteError = m.integrations.error?.code\n"
            "m.pickIntegrationsHost(null)\n"
            "return { remoteError, localError: m.integrations.error }"
        )
        assert got["remoteError"] == "auth.role_forbidden" and got["localError"] is None


class TestAPublishOnARemoteInstanceUsesThatHostsSettings:
    def test_each_hosts_store_options_preflight_and_drift_check_are_its_own(self, node):
        got = node(
            f"const p = {_mod('publish.js')}\n"
            "globalThis.__t.answers['/api/v1/hosts/remote/publications'] = { sites: { 'site.example.com': { project: 'p', bench_path: '/b', url: 'https://site.example.com', parts: {} } }, dns: null, proxy: { id: 'npm', name: 'Nginx Proxy Manager' } }\n"
            "globalThis.__t.answers['/api/v1/publications'] = { sites: {}, dns: null, proxy: null }\n"
            "globalThis.__t.answers['/api/v1/hosts/remote/publish/check'] = { ok: true, parts: {} }\n"
            "await p.loadPublications()\n"
            "await p.loadPublications('remote')\n"
            "await p.publishOptions('remote')\n"
            "await p.preflightPublish({ project: 'p', bench: 0, site: 'site.example.com' }, 'remote')\n"
            "await p.checkSite('site.example.com', 'remote')\n"
            "await p.publishOptions()\n"
            "return { calls: globalThis.__t.calls.map((c) => c.slice(0, 2)), remoteCan: p.canPublish('remote'), localCan: p.canPublish(),\n"
            "  at: p.publicationAt('site.example.com', 'p', '/b', p.publicationsFor('remote'))?.url ?? null,\n"
            "  localAt: p.publicationAt('site.example.com', 'p', '/b') ?? null, unknownHost: p.publicationsFor('other'), toasts: globalThis.__t.toasts.map((t) => t.title) }"
        )
        assert got["calls"] == [
            ["GET", "/api/v1/publications"],
            ["GET", "/api/v1/hosts/remote/publications"],
            ["GET", "/api/v1/hosts/remote/publish/options"],
            ["POST", "/api/v1/hosts/remote/publish/preflight"],
            ["POST", "/api/v1/hosts/remote/publish/check"],
            ["GET", "/api/v1/publish/options"],
        ]
        # The remote can publish (its proxy is on); this machine cannot; the stores never mix.
        assert got["remoteCan"] is True and got["localCan"] is False
        assert got["at"] == "https://site.example.com" and got["localAt"] is None
        assert got["unknownHost"] is None
        assert got["toasts"] == ["site.example.com: no drift"]

    def test_a_remote_store_that_cannot_be_read_says_so_instead_of_reading_as_unpublishable(
        self, node
    ):
        got = node(
            f"const p = {_mod('publish.js')}\n"
            "globalThis.__t.answers['/api/v1/hosts/remote/publications'] = Object.assign(new Error('remote.example.com is unreachable; reading it is not available.'), { code: 'host.not_connected' })\n"
            "await p.loadPublications('remote')\n"
            "const s = p.publicationsFor('remote')\n"
            "return { error: s?.error?.code ?? null, loaded: s?.loaded, can: p.canPublish('remote') }"
        )
        assert got == {"error": "host.not_connected", "loaded": False, "can": False}

    def test_the_stream_lands_a_remotes_publications_in_that_hosts_store(self, node):
        got = node(
            f"const p = {_mod('publish.js')}\n"
            "p.publicationsLanded({ sites: {}, dns: { id: 'cloudflare', name: 'Cloudflare DNS' }, proxy: null }, 'remote')\n"
            "p.publicationsLanded({ sites: {}, dns: null, proxy: null })\n"
            "return { remote: p.publicationsFor('remote').dns?.id ?? null, local: p.publications.dns, can: p.canPublish('remote') }"
        )
        assert got == {"remote": "cloudflare", "local": None, "can": True}

    @pytest.mark.parametrize(
        ("options", "says"),
        [
            (
                None,
                "a second job on remote.example.com publishes it at its own domain, with remote.example.com's own integration settings.",
            ),
            (
                {"dns": {"name": "Cloudflare DNS"}, "proxy": {"name": "Nginx Proxy Manager"}},
                "a second job (Publish site) on remote.example.com, with remote.example.com's own Cloudflare DNS and Nginx Proxy Manager settings, writes its Cloudflare DNS A record, makes its Nginx Proxy Manager proxy host",
            ),
            (
                {"dns": {"name": "Cloudflare DNS"}, "proxy": None},
                "a second job on remote.example.com, with remote.example.com's own Cloudflare DNS settings, writes its Cloudflare DNS A record (DNS only)",
            ),
            (
                {"dns": None, "proxy": {"name": "Nginx Proxy Manager"}},
                "the name must already point at remote.example.com.",
            ),
        ],
    )
    def test_new_sites_sentence_names_the_host_whose_settings_the_publish_uses(
        self, node, options, says
    ):
        got = node(
            f"const p = {_mod('publish.js')}\n"
            f"return [p.publishPlan({json.dumps(options)}, 'remote.example.com'), p.publishPlan({json.dumps(options)})]"
        )
        assert says in got[0]
        # This machine's own instance: the sentence as before, naming no host.
        assert (
            "remote.example.com" not in got[1]
            and "with " not in got[1].split("a second job")[1].split("writes")[0]
        )
        if options and options["dns"] is None:
            assert got[1].endswith("point at this machine.")

    def test_whose_settings_as_a_phrase(self, node):
        got = node(
            f"const p = {_mod('publish.js')}\n"
            "const name = (h) => ({ remote: 'remote.example.com' })[h]\n"
            "return [p.settingsOwner('remote', name), p.settingsOwner(null, name)]"
        )
        assert got == ["remote.example.com's own", "this machine's"]
