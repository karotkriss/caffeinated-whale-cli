"""The web UI's typed confirm for a forced frappe update, executed under node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "force-confirm.js"
BENCH = {"project": "acme", "bench": "1", "benchName": "staging"}

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _decide(values, context, typed=""):
    script = (
        f"import {{ forceConfirmTarget, forceConfirmed }} from {json.dumps(MODULE.as_uri())};\n"
        f"const [v, c, t] = {json.dumps([values, context, typed])};\n"
        "console.log(JSON.stringify([forceConfirmTarget(v, c), forceConfirmed(v, c, t)]));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_forced_frappe_update_requires_the_bench_name():
    values = {"force": True, "apps": "erpnext, Frappe"}
    assert _decide(values, BENCH) == ["staging", False]
    assert _decide(values, BENCH, "stag") == ["staging", False]
    assert _decide(values, BENCH, "  staging ") == ["staging", True]


def test_retried_job_with_list_apps_and_no_bench_label_types_the_bench_index():
    values = {"force": True, "apps": ["frappe"]}
    assert _decide(values, {"project": "acme", "bench": "0"}, "0") == ["0", True]


@pytest.mark.parametrize(
    "values",
    [
        {"force": False, "apps": "frappe"},
        {"force": True, "apps": "erpnext, payments"},
        {"force": True, "apps": ""},
    ],
)
def test_no_typed_confirm_without_force_on_the_frappe_path(values):
    assert _decide(values, BENCH) == ["", True]
