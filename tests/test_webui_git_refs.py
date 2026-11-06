"""The shared branch/tag autocomplete model, executed under Node.

The real module resolves the repository that an app action targets, asks that
machine's existing refs endpoint, groups branches and tags, and always keeps a
custom free-text row for commit hashes and failed lookups.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "webui" / "src" / "data"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUBS = {
    "vue": """
export const ref = (value) => ({ value })
export const toValue = (source) => (typeof source === 'function' ? source() : source?.value ?? source)
export const computed = (fn) => ({ get value() { return fn() } })
export const watch = (source, callback, options) => {
  globalThis.__triggerWatch = () => callback(source())
  if (options?.immediate) callback(source())
}
export const onUnmounted = () => {}
""",
    "./api": """
export const calls = []
export const apiBase = (host) => host ? `/api/v1/hosts/${encodeURIComponent(host)}` : '/api/v1'
export const getJson = async (url) => {
  calls.push(url)
  if (globalThis.__refsError) throw new Error(globalThis.__refsError)
  return globalThis.__refsResult
}
""",
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


def test_repository_follows_the_one_app_or_url_the_field_acts_on(node):
    result = node(
        f"const refs = await import({json.dumps((DATA / 'git-refs.js').as_uri())})\n"
        "const bench = { app_remotes: { frappe: ['https://git.example/frappe.git'] } }\n"
        "const apps = [{ name: 'payments', source: { url: 'https://git.example/payments' } }]\n"
        "return {\n"
        "  direct: refs.repoForAppInput('https://git.example/direct', { bench, apps }),\n"
        "  installed: refs.repoForAppInput('frappe', { bench, apps }),\n"
        "  catalog: refs.repoForAppInput('payments', { bench, apps }),\n"
        "  several: refs.repoForAppInput('frappe, payments', { bench, apps }),\n"
        "  missing: refs.repoForAppInput('missing', { bench, apps }),\n"
        "}"
    )
    assert result == {
        "direct": "https://git.example/direct",
        "installed": "https://git.example/frappe.git",
        "catalog": "https://git.example/payments",
        "several": None,
        "missing": None,
    }


def test_refs_come_from_the_acting_host_and_keep_a_commit_hash_row(node):
    result = node(
        "globalThis.__refsResult = { ok: true, branches: ['main', 'version-16'], tags: ['v16.1.0'] }\n"
        f"const refsModule = await import({json.dumps((DATA / 'git-refs.js').as_uri())})\n"
        "const api = await import('./api')\n"
        "let chosen = null\n"
        "const model = refsModule.useGitRefs(\n"
        "  () => 'https://git.example/acme/app.git',\n"
        "  (query) => { chosen = query },\n"
        "  { host: () => 'build box' },\n"
        ")\n"
        "await new Promise((resolve) => setTimeout(resolve, 0))\n"
        "const custom = model.options.value.find((option) => option.type === 'custom')\n"
        "custom.onClick({ query: 'deadbeef' })\n"
        "return {\n"
        "  calls: api.calls,\n"
        "  groups: model.options.value.filter((option) => option.group).map((option) => [option.group, option.options.map((item) => item.value)]),\n"
        "  chosen,\n"
        "  listedCommitIsCustom: custom.condition({ query: 'deadbeef' }),\n"
        "  listedBranchIsCustom: custom.condition({ query: 'main' }),\n"
        "}"
    )
    assert result == {
        "calls": [
            "/api/v1/hosts/build%20box/apps/refs?url=https%3A%2F%2Fgit.example%2Facme%2Fapp.git"
        ],
        "groups": [
            ["Branches", ["main", "version-16"]],
            ["Tags", ["v16.1.0"]],
        ],
        "chosen": "deadbeef",
        "listedCommitIsCustom": True,
        "listedBranchIsCustom": False,
    }


def test_failed_lookup_still_accepts_free_text(node):
    result = node(
        "globalThis.__refsError = 'repository is offline'\n"
        f"const refsModule = await import({json.dumps((DATA / 'git-refs.js').as_uri())})\n"
        "let chosen = null\n"
        "const model = refsModule.useGitRefs(\n"
        "  () => 'https://git.example/acme/app.git',\n"
        "  (query) => { chosen = query },\n"
        ")\n"
        "await new Promise((resolve) => setTimeout(resolve, 0))\n"
        "const custom = model.options.value.find((option) => option.type === 'custom')\n"
        "custom.onClick({ query: '0123456789abcdef' })\n"
        "return { error: model.refs.value.error, empty: model.emptyText.value, chosen }"
    )
    assert result == {
        "error": "repository is offline",
        "empty": "refs not listed: repository is offline",
        "chosen": "0123456789abcdef",
    }
