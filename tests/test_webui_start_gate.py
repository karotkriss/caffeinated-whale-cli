"""The page's "start it first?" gate starts the bench the page already knows, run under node.

On a site of a stopped multi-bench instance, Open site asked to start the instance,
then failed with a "needs choice" error naming every bench: the gate started the
instance without the bench, and ``core.start`` cannot pick a bench for a
multi-bench instance by itself. Every verb launched from a bench, site or app row
now hands its bench to the gate, so the start the gate runs is
``cwcli start <p> --bench <n>`` and no choice is ever needed.

The real ``dialog.js``, ``job-dialog.js``, ``start-gate.js``, ``hosts.js`` and
``publish.js`` run here; only their browser-side imports are stubbed, and the stubbed
job store answers a bench-less start on a multi-bench instance with ``needs_choice``,
the way ``core.start`` does.
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli import jobs
from caffeinated_whale_cli.core.envelope import Result, Status

DATA = Path(__file__).resolve().parents[1] / "webui" / "src" / "data"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUBS = {
    "vue": "export const reactive = (o) => o\nexport const computed = (fn) => ({ get value() { return fn() } })",
    "frappe-ui": (
        "const record = (kind) => (title, options) => globalThis.__t.toasts.push({ kind, title, options })\n"
        "export const toast = { error: record('error'), success: record('success'), warning: record('warning') }"
    ),
    "./session": (
        "export const can = () => true\n"
        "export const refuseBelow = () => false\n"
        "export const ROLE_LABEL = {}\n"
        "export const session = { user: { role: 'admin' } }"
    ),
    "./job-toasts": "export const trackJob = () => {}",
    # This machine's acme, and a same-named instance on the remote host `box`.
    "./fleet": (
        "export const details = {\n"
        "  acme: { data: { benches: [{ index: 0, path: '/workspace/bench-a' }, { index: 1, path: '/workspace/bench-b' }] } },\n"
        "  'box/acme': { data: { benches: [{ index: 1, path: '/srv/bench-b' }] } },\n"
        "}\n"
        "export const detailOf = (project, host = null) => details[host ? `${host}/${project}` : project] || null\n"
        "export const instance = (p) => globalThis.__t.instances[p]\n"
        "export const loadDetail = () => {}\n"
        "export async function getJsonUrl(project, query) {\n"
        "  globalThis.__t.calls.push({ url: project, query: Object.fromEntries(query) })\n"
        "  return { url: globalThis.__t.probeUrl ?? 'http://b.localhost:12101', reachable: true, ...globalThis.__t.probe }\n"
        "}"
    ),
    "./api": (
        "export const apiBase = (host) => (host ? `/api/v1/hosts/${host}` : '/api/v1')\n"
        "export async function getJson(url) {\n"
        "  globalThis.__t.reads.push(url)\n"
        "  if (!(url in globalThis.__t.answers)) throw new Error(`${url} is not readable`)\n"
        "  return globalThis.__t.answers[url]\n"
        "}\n"
        "export async function postJson(url, payload) {\n"
        "  globalThis.__t.calls.push({ post: payload })\n"
        "  return { outcome: {} }\n"
        "}"
    ),
    # core.start's rule: without a bench, a multi-bench instance is a select_bench choice.
    # core.rm_bench's rule: a bench that a start brought up is refused as running.
    "./jobs": (
        "export const ACTIVE_STATES = new Set(['queued', 'running'])\n"
        "export const JOB_LABEL = new Proxy({}, { get: (_, verb) => String(verb) })\n"
        "export async function createJob(spec, host) {\n"
        "  const t = globalThis.__t\n"
        "  t.calls.push(host ? { job: spec, host } : { job: spec })\n"
        "  const inst = t.instances[spec.target.project]\n"
        "  const id = `job${t.calls.length}`\n"
        "  t.states[id] = 'succeeded'\n"
        "  if (spec.verb === 'start') {\n"
        "    if (spec.args.containers_only) {}\n"
        "    else if (spec.target.bench == null && inst.benches.length > 1) t.states[id] = 'needs_choice'\n"
        "    else t.started.push(String(spec.target.bench ?? 0))\n"
        "    inst.container_running = true\n"
        "  }\n"
        "  if (spec.verb === 'rm-bench' && t.started.includes(String(spec.target.bench))) t.states[id] = 'failed'\n"
        "  if (spec.verb === 'restart' && spec.target.bench == null) t.states[id] = t.restartState\n"
        "  return { id }\n"
        "}\n"
        "export async function loadJob(id) { return { id, state: globalThis.__t.states[id] } }"
    ),
}

HOOKS = """
const STUBS = %s
export async function resolve(specifier, context, next) {
  if (specifier in STUBS) return { url: `stub:${specifier}`, shortCircuit: true }
  // Vite resolves the page's extensionless imports; node needs the file name.
  if (specifier.startsWith('.') && !/\\.\\w+$/.test(specifier)) specifier += '.js'
  return next(specifier, context)
}
export async function load(url, context, next) {
  if (url.startsWith('stub:')) return { format: 'module', source: STUBS[url.slice(5)], shortCircuit: true }
  return next(url, context)
}
"""

STOPPED_TWO_BENCHES = {
    "acme": {
        "project": "acme",
        "container_running": False,
        "benches": [{"index": 0, "overall": "stopped"}, {"index": 1, "overall": "stopped"}],
    }
}
SITE = {"project": "acme", "bench": "1", "benchName": "bench-b", "site": "b.localhost"}
BENCH = {"project": "acme", "bench": "1", "benchName": "bench-b"}


@pytest.fixture
def run(tmp_path):
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(HOOKS % json.dumps(STUBS))
    register = tmp_path / "register.mjs"
    register.write_text(
        f"import {{ register }} from 'node:module'\nregister({json.dumps(hooks.as_uri())})\n"
    )

    def _run(body, instances=STOPPED_TWO_BENCHES, restart_state="needs_choice"):
        script = (
            f"globalThis.__t = {{ instances: {json.dumps(instances)}, restartState: {json.dumps(restart_state)}, calls: [], states: {{}}, started: [], toasts: [], reads: [], answers: {{}} }}\n"
            "globalThis.window = { open(url) { globalThis.__t.calls.push({ open: url }) } }\n"
            f"const dialog = await import({json.dumps((DATA / 'dialog.js').as_uri())})\n"
            f"const jobDialog = await import({json.dumps((DATA / 'job-dialog.js').as_uri())})\n"
            f"const gate = await import({json.dumps((DATA / 'start-gate.js').as_uri())})\n"
            f"const hosts = await import({json.dumps((DATA / 'hosts.js').as_uri())})\n"
            f"const publish = await import({json.dumps((DATA / 'publish.js').as_uri())})\n"
            f"const {{ ACTIONS }} = await import({json.dumps((DATA / 'actions.js').as_uri())})\n"
            # The remote host `box`, connected on a matching protocol.
            "hosts.replaceHosts([{ id: 'box', name: 'Build box', state: 'connected', actions_allowed: true, readable: true }])\n"
            # The page's own Start button, then let the verb that waited on it finish.
            "async function startThen(pending) {\n"
            "  const opened = gate.startGate.open\n"
            "  if (opened) await gate.answerStart(true)\n"
            "  const done = await Promise.race([pending, new Promise((r) => setTimeout(() => r('pending'), 300))])\n"
            "  return { opened, done: done !== 'pending', error: gate.startGate.error }\n"
            "}\n"
            f"const out = await (async () => {{ {body} }})()\n"
            "console.log(JSON.stringify({ ...out, calls: globalThis.__t.calls, states: globalThis.__t.states, toasts: globalThis.__t.toasts, reads: globalThis.__t.reads }))\n"
            "process.exit(0)\n"
        )
        result = subprocess.run(
            ["node", "--import", register.as_uri(), "--input-type=module"],
            input=script,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    return _run


def _starts(out):
    return [c["job"]["target"] for c in out["calls"] if c.get("job", {}).get("verb") == "start"]


def _assert_started_the_bench_then_ran(out):
    assert out["opened"], "a stopped instance must ask before starting"
    assert _starts(out) == [{"project": "acme", "bench": "1"}]
    assert out["error"] is None
    assert out["done"], "the verb must carry on once its bench is up"


def test_open_site_starts_the_sites_bench_then_opens_it(run):
    out = run(f"return startThen(dialog.runAction('site.open', {json.dumps(SITE)}, {{}}))")
    _assert_started_the_bench_then_ran(out)
    assert out["calls"][-2:] == [
        {"url": "acme", "query": {"bench": "1", "site": "b.localhost"}},
        {"open": "http://b.localhost:12101"},
    ]


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/apps/refs?url=https://attacker.example/x",
        "javascript:alert(1)",
        "//attacker.example/x",
        "",
    ],
)
def test_open_site_opens_only_an_http_address(run, url):
    """A remote host answers the site's address; anything but an http(s) URL would send
    this page to a route of the hub itself (or run as script), so nothing is opened."""
    up = {
        "acme": {
            "project": "acme",
            "container_running": True,
            "benches": [{"index": 0, "overall": "running"}, {"index": 1, "overall": "running"}],
        }
    }
    out = run(
        f"globalThis.__t.probeUrl = {json.dumps(url)}\n"
        f"return {{ probe: await dialog.runAction('site.open', {json.dumps(SITE)}, {{}}) }}",
        instances=up,
    )
    assert out["probe"] is None
    assert not [c for c in out["calls"] if "open" in c]
    assert [t["title"] for t in out["toasts"]] == ["b.localhost has no address to open"]


def test_a_tab_the_browser_would_block_after_the_start_is_offered_on_a_button(run):
    # The start outlives the click's user activation, so window.open would be blocked
    # without a word; the page offers the tab on a toast button instead.
    out = run(
        "Object.defineProperty(globalThis, 'navigator', { value: { userActivation: { isActive: false } } })\n"
        f"const out = await startThen(dialog.runAction('site.open', {json.dumps(SITE)}, {{}}))\n"
        "const offered = globalThis.__t.toasts.at(-1)\n"
        "const before = globalThis.__t.calls.filter((c) => c.open).length\n"
        "offered.options.action.onClick()\n"
        "return { ...out, before, offered: { title: offered.title, label: offered.options.action.label } }"
    )
    _assert_started_the_bench_then_ran(out)
    assert out["before"] == 0
    assert out["offered"] == {"title": "b.localhost is ready", "label": "Open"}
    assert out["calls"][-1] == {"open": "http://b.localhost:12101"}


@pytest.mark.parametrize(
    ("action", "ctx", "values"),
    [
        ("site.unlock", SITE, {}),
        ("site.dev_mode_on", SITE, {}),
        ("site.dev_mode_off", SITE, {}),
        ("site.maintenance_off", SITE, {}),
        ("site.remove", SITE, {}),
        ("bench.restart", BENCH, {}),
        ("bench.label", BENCH, {"label": "v15-main"}),
        ("process.restart", {**BENCH, "process": "web"}, {}),
        ("app.checkout", {**BENCH, "app": "crm"}, {"ref": "develop"}),
        ("app.checkout_reset", {**BENCH, "app": "crm"}, {"ref": "develop"}),
    ],
)
def test_every_bench_and_site_action_starts_its_own_bench(run, action, ctx, values):
    out = run(
        f"return startThen(dialog.runAction({json.dumps(action)}, {json.dumps(ctx)}, "
        f"{json.dumps(values)}))"
    )
    _assert_started_the_bench_then_ran(out)
    verb_call = out["calls"][-1]
    sent = verb_call.get("job") or verb_call["post"]
    assert str(sent.get("target", sent).get("bench")) == "1"


def test_remove_bench_never_starts_the_bench_it_removes(run):
    # The rm-bench job brings up only the containers; a started bench would be refused.
    out = run(f"return startThen(dialog.runAction('bench.remove', {json.dumps(BENCH)}, {{}}))")
    assert out["opened"] is False
    assert _starts(out) == []
    assert out["done"]
    assert [c["job"] for c in out["calls"]] == [
        {
            "verb": "rm-bench",
            "target": {"project": "acme", "bench": "1"},
            "args": {},
            "consent": True,
        }
    ]
    assert list(out["states"].values()) == ["succeeded"]


@pytest.mark.parametrize(
    ("verb", "ctx"),
    [
        ("backup", SITE),
        ("migrate", SITE),
        ("run-tests", SITE),
        ("setup-wizard", SITE),
        ("backup", BENCH),
        ("migrate", BENCH),
        ("build", BENCH),
        ("apps-install", BENCH),
        ("apps-update", BENCH),
    ],
)
def test_every_job_from_a_bench_or_site_starts_its_own_bench(run, verb, ctx):
    out = run(
        f"jobDialog.openJob({json.dumps(verb)}, {json.dumps(ctx)})\n"
        "return startThen(jobDialog.submitJob())"
    )
    _assert_started_the_bench_then_ran(out)
    assert out["calls"][-1]["job"]["verb"] == verb
    assert out["calls"][-1]["job"]["target"]["bench"] == "1"


def _real_bench_less_restart_state(monkeypatch):
    """What the real restart job ends as on the two-bench instance the gate just brought up."""
    frappe = SimpleNamespace(status="running", labels={"com.docker.compose.service": "frappe"})
    monkeypatch.setattr(
        jobs.core_resolvers,
        "cached_benches",
        lambda project: [{"path": "/workspace/a"}, {"path": "/workspace/b"}],
    )
    monkeypatch.setattr(jobs.core_docker, "get_frappe_container", lambda project: frappe)
    monkeypatch.setattr(
        jobs.core_supervision,
        "discover_stack",
        lambda container, path, required=False: SimpleNamespace(supervisor_up=False),
    )
    monkeypatch.setattr(
        jobs.core_supervision,
        "discover_unsupervised_stack",
        lambda container, path, required=False: SimpleNamespace(manager_up=False),
    )
    monkeypatch.setattr(jobs.core_stop, "stop", lambda project, **k: Result(status=Status.OK))
    monkeypatch.setattr(jobs.core_start, "resolve_project_containers", lambda project: [frappe])
    monkeypatch.setattr(jobs, "_start_containers_only", lambda project, containers: [])
    monkeypatch.setattr(jobs.core_start, "start", lambda *a, **k: pytest.fail("started a bench"))
    result = jobs._restart_instance({"project": "acme"}, lambda event: None)
    if result.status is Status.NEEDS_CHOICE:
        return "needs_choice"
    return "succeeded" if jobs._result_ok(result) else "failed"


@pytest.mark.parametrize("action", ["instance.scale", "instance.restart", "instance.remove"])
def test_an_instance_action_starts_only_the_containers_never_a_bench(run, action, monkeypatch):
    out = run(
        f"return startThen(dialog.runAction({json.dumps(action)}, {{ project: 'acme' }}, {{}}))",
        restart_state=_real_bench_less_restart_state(monkeypatch),
    )
    assert out["opened"]
    starts = [c["job"] for c in out["calls"] if c.get("job", {}).get("verb") == "start"]
    assert starts == [
        {"verb": "start", "target": {"project": "acme"}, "args": {"containers_only": True}}
    ]
    assert out["error"] is None
    assert out["done"], "the instance verb must carry on once the containers are up"
    assert out["calls"][-1]["job"]["verb"] != "start"
    assert set(out["states"].values()) == {"succeeded"}


def test_new_bench_starts_only_the_containers(run):
    out = run("return startThen(gate.ensureRunning('acme', 'New bench'))")
    assert _starts(out) == [{"project": "acme"}]
    assert out["calls"][0]["job"]["args"] == {"containers_only": True}
    assert out["error"] is None
    assert out["done"]


def test_a_running_instance_asks_about_a_stopped_bench_only_when_the_verb_needs_it_serving(run):
    up = {
        "acme": {
            "project": "acme",
            "container_running": True,
            "benches": [{"index": 0, "overall": "running"}, {"index": 1, "overall": "stopped"}],
        }
    }
    out = run(
        "const plain = gate.ensureRunning('acme', 'Migrate', { bench: '1' })\n"
        "const plainOpened = gate.startGate.open\n"
        "await plain\n"
        "gate.ensureRunning('acme', 'Install apps', { bench: '1', serving: true })\n"
        "return { plainOpened, servingOpened: gate.startGate.open }",
        instances=up,
    )
    assert out["plainOpened"] is False
    assert out["servingOpened"] is True
    assert out["calls"] == []


RUNNING = {"acme": {**STOPPED_TWO_BENCHES["acme"], "container_running": True}}
# What the drop dialog shows for a ctx: its shown fields' notes, as ActionDialog renders them.
DROP = (
    "const { ctx, values: dialogValues } = dialog.dialogState\n"
    "const values = { ...dialogValues }\n"
    "const shown = ACTIONS['site.remove'].fields.filter((f) => !f.show || f.show(ctx))\n"
    "const notes = shown.map((f) => f.note?.(ctx)).filter(Boolean)\n"
    "const ticked = ACTIONS['site.remove'].request(ctx, { unpublish: true }).args\n"
    "await dialog.runAction('site.remove', ctx, values)\n"
    "return { values, notes, ticked, shown: shown.map((f) => f.name), published: ctx.published }"
)


@pytest.mark.parametrize(
    ("bench_path", "published"),
    [("/workspace/bench-b", True), ("/workspace/bench-a", False), (None, False)],
)
def test_dropping_a_published_site_offers_also_unpublish_ticked(run, bench_path, published):
    sites = {"b.localhost": {"project": "acme", "bench_path": bench_path}} if bench_path else {}
    out = run(
        f"publish.publicationsLanded({{ sites: {json.dumps(sites)}, dns: {{ name: 'Cloudflare DNS' }} }})\n"
        f"dialog.openAction('site.remove', {json.dumps(SITE)})\n" + DROP,
        instances=RUNNING,
    )
    assert out["published"] is published
    assert out["values"] == ({"unpublish": True} if published else {})
    assert out["notes"] == []
    assert out["calls"][-1] == {
        "job": {
            "verb": "rm-site",
            "target": {"project": "acme", "bench": "1", "site": "b.localhost"},
            "args": {"unpublish": True} if published else {},
            "consent": True,
        }
    }
    assert out["reads"] == []


PUBLISHED_THERE = {
    "sites": {"b.localhost": {"project": "acme", "bench_path": "/srv/bench-b"}},
    "dns": {"name": "Box DNS"},
}


@pytest.mark.parametrize(
    ("answer", "detail", "published"),
    [
        (PUBLISHED_THERE, True, True),
        # The same name published on that host from a different bench is not this site.
        (
            {"sites": {"b.localhost": {"project": "acme", "bench_path": "/workspace/bench-b"}}},
            True,
            False,
        ),
        (None, True, None),  # the host could not be read
        # The remote's bench detail has not arrived (or failed), so its path is not known.
        (PUBLISHED_THERE, False, None),
    ],
)
def test_dropping_a_remote_site_asks_its_own_host_whether_it_is_published(
    run, answer, detail, published
):
    """Publishing is each machine's own: a remote site's drop dialog reads that host's
    publications once, through the hub's forward, and sends the job's unpublish to the
    daemon that made them. This machine's store (which here names the same site) is never
    consulted, and a host that cannot be read, or a bench whose path the page does not
    know yet, leaves the box offered, unticked, with a note."""
    remote = {**SITE, "host": "box"}
    out = run(
        "publish.publicationsLanded({ sites: { 'b.localhost': { project: 'acme', bench_path: '/srv/bench-b' } } })\n"
        + (
            f"globalThis.__t.answers['/api/v1/hosts/box/publications'] = {json.dumps(answer)}\n"
            if answer
            else ""
        )
        + ("" if detail else "delete (await import('./fleet')).details['box/acme']\n")
        + f"await dialog.openAction('site.remove', {json.dumps(remote)})\n"
        + DROP,
        instances=RUNNING,
    )
    assert out["reads"] == (["/api/v1/hosts/box/publications"] if detail else [])
    assert out["published"] is published
    assert ("unpublish" in out["shown"]) is (published is not False)
    assert (
        out["values"]
        == {True: {"unpublish": True}, False: {}, None: {"unpublish": False}}[published]
    )
    assert out["notes"] == (
        []
        if published is not None
        else [
            "Whether b.localhost is published could not be read from its machine; tick this if it is."
        ]
    )
    assert out["ticked"] == ({} if published is False else {"unpublish": True})
    job = out["calls"][-1]
    assert job["host"] == "box" and job["job"]["verb"] == "rm-site"
    assert job["job"]["args"] == ({"unpublish": True} if published else {})


@pytest.mark.parametrize(
    ("verb", "ctx", "title"),
    [
        ("init", {"host": "box"}, "Create an instance on Build box"),
        ("init", {}, "Create an instance"),
        ("migrate", {**SITE, "host": "box"}, "Migrate site on Build box"),
        ("migrate", SITE, "Migrate site"),
        ("apps-update", {**BENCH, "host": "box"}, "Update apps on Build box"),
    ],
)
def test_the_job_dialog_names_the_machine_a_remote_job_runs_on(run, verb, ctx, title):
    out = run(
        f"jobDialog.openJob({json.dumps(verb)}, {json.dumps(ctx)})\n"
        "return { open: jobDialog.jobDialog.open, title: jobDialog.jobDialogTitle() }"
    )
    assert out["open"] and out["title"] == title


# --- Open site on an address that carries the bench's internal port ---------------
#
# While webserver_port is set, Frappe builds a site's URLs with the bench's own web
# port, and cwcli keeps that port: it is what Frappe reports, and a *.localhost
# development site needs it. A site named by a real hostname is usually served
# through a reverse proxy, where that port is internal and the address fails, so the
# daemon flags it (`internal_port`) and the page asks first, with the advice to
# remove the key, instead of opening a dead tab or rewriting the address.

RUNNING_ONE_BENCH = {
    "acme": {
        "project": "acme",
        "container_running": True,
        "benches": [{"index": 0, "overall": "running"}],
    }
}
HOSTNAME_SITE = {
    "project": "acme",
    "bench": "0",
    "benchName": "bench-a",
    "site": "site.example.com",
}
FIX = [
    {
        "step": "Remove webserver_port",
        "command": "cwcli run acme config remove-common-config webserver_port",
    },
]
INTERNAL = {
    "url": "http://site.example.com:8000",
    "site": "site.example.com",
    "internal_port": True,
    "webserver_port_in": ["sites/common_site_config.json"],
    "fix": FIX,
}

# Open the site, let the probe settle, then answer the warning (when one is asked).
OPEN_THEN = """
globalThis.__t.probe = %s
const pending = dialog.runAction('site.open', %s, {})
await new Promise((r) => setTimeout(r, 50))
const asked = dialog.portWarning.open
const shown = dialog.portWarning.probe
const before = globalThis.__t.calls.filter((c) => c.open).length
if (asked) dialog.answerPortWarning(%s)
const probe = await pending
return { asked, shown, before, probe }
"""


def _open(run, probe, *, answer):
    return run(
        OPEN_THEN % (json.dumps(probe), json.dumps(HOSTNAME_SITE), json.dumps(answer)),
        instances=RUNNING_ONE_BENCH,
    )


def _opened(out):
    return [c["open"] for c in out["calls"] if "open" in c]


def test_an_address_with_the_internal_port_asks_before_opening(run):
    out = _open(run, INTERNAL, answer=True)

    assert out["asked"] is True
    assert out["before"] == 0, "nothing opens before the warning is answered"
    # The dialog shows the address as Frappe reports it, with the removal advice.
    assert out["shown"]["url"] == "http://site.example.com:8000"
    assert out["shown"]["fix"] == FIX
    # Open anyway keeps the port: cwcli warns, it never rewrites the address.
    assert _opened(out) == ["http://site.example.com:8000"]


def test_cancelling_the_warning_opens_nothing(run):
    out = _open(run, INTERNAL, answer=False)

    assert out["asked"] is True
    assert out["probe"] is None
    assert _opened(out) == []


def test_a_localhost_address_opens_with_its_port_and_no_warning(run):
    probe = {"url": "http://dev.localhost:8000", "internal_port": False, "fix": []}

    out = _open(run, probe, answer=True)

    assert out["asked"] is False
    assert _opened(out) == ["http://dev.localhost:8000"]


def test_a_hostname_without_the_key_opens_by_name_and_no_warning(run):
    probe = {"url": "http://site.example.com", "internal_port": False, "fix": []}

    out = _open(run, probe, answer=True)

    assert out["asked"] is False
    assert _opened(out) == ["http://site.example.com"]


def test_a_host_name_with_the_appended_port_asks_before_opening(run):
    probe = {**INTERNAL, "url": "https://site.example.com:8000", "source": "host_name"}

    out = _open(run, probe, answer=True)

    assert out["asked"] is True
    assert _opened(out) == ["https://site.example.com:8000"]


def test_a_site_with_no_address_shows_the_probes_own_reason(run):
    reason = (
        "The site's address http://dev.localhost:31337 names a port this instance does not publish."
    )
    out = _open(run, {"url": None, "reachable": False, "reason": reason}, answer=True)

    assert out["probe"] is None
    assert _opened(out) == []
    assert [t["options"]["description"] for t in out["toasts"]] == [reason]
