"""The page's "start it first?" gate starts the bench the page already knows, run under node.

On a site of a stopped multi-bench instance, Open site asked to start the instance,
then failed with a "needs choice" error naming every bench: the gate started the
instance without the bench, and ``core.start`` cannot pick a bench for a
multi-bench instance by itself. Every verb launched from a bench, site or app row
now hands its bench to the gate, so the start the gate runs is
``cwcli start <p> --bench <n>`` and no choice is ever needed.

The real ``dialog.js``, ``job-dialog.js`` and ``start-gate.js`` run here; only their
browser-side imports are stubbed, and the stubbed job store answers a bench-less
start on a multi-bench instance with ``needs_choice``, the way ``core.start`` does.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "webui" / "src" / "data"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUBS = {
    "vue": "export const reactive = (o) => o",
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
    "../router": "export const router = { push() {} }",
    "./fleet": (
        "export const instance = (p) => globalThis.__t.instances[p]\n"
        "export const loadDetail = () => {}\n"
        "export async function getJsonUrl(project, query) {\n"
        "  globalThis.__t.calls.push({ url: project, query: Object.fromEntries(query) })\n"
        "  return { url: 'http://b.localhost:12101', reachable: true }\n"
        "}"
    ),
    "./api": (
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
        "export async function createJob(spec) {\n"
        "  const t = globalThis.__t\n"
        "  t.calls.push({ job: spec })\n"
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

    def _run(body, instances=STOPPED_TWO_BENCHES):
        script = (
            f"globalThis.__t = {{ instances: {json.dumps(instances)}, calls: [], states: {{}}, started: [], toasts: [] }}\n"
            "globalThis.window = { open(url) { globalThis.__t.calls.push({ open: url }) } }\n"
            f"const dialog = await import({json.dumps((DATA / 'dialog.js').as_uri())})\n"
            f"const jobDialog = await import({json.dumps((DATA / 'job-dialog.js').as_uri())})\n"
            f"const gate = await import({json.dumps((DATA / 'start-gate.js').as_uri())})\n"
            # The page's own Start button, then let the verb that waited on it finish.
            "async function startThen(pending) {\n"
            "  const opened = gate.startGate.open\n"
            "  if (opened) await gate.answerStart(true)\n"
            "  const done = await Promise.race([pending, new Promise((r) => setTimeout(() => r('pending'), 300))])\n"
            "  return { opened, done: done !== 'pending', error: gate.startGate.error }\n"
            "}\n"
            f"const out = await (async () => {{ {body} }})()\n"
            "console.log(JSON.stringify({ ...out, calls: globalThis.__t.calls, states: globalThis.__t.states, toasts: globalThis.__t.toasts }))\n"
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


@pytest.mark.parametrize("action", ["instance.scale", "instance.restart", "instance.remove"])
def test_an_instance_action_starts_only_the_containers_never_a_bench(run, action):
    out = run(
        f"return startThen(dialog.runAction({json.dumps(action)}, {{ project: 'acme' }}, {{}}))"
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
