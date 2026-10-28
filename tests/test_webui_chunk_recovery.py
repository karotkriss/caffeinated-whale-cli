"""A long-open tab recovers from a rebuild that replaced its route chunks.

The captain could not open any job's detail page in a tab that had been open
across several rebuilds: the Job route's chunk was lazily loaded, the file it was
built against had been replaced, and the navigation failed. The page now reloads
itself once for the page the user asked for (``chunk-recovery.js``), and the
build keeps earlier chunks on disk (``emptyOutDir: false``).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parents[1] / "webui"
MODULE = WEBUI / "src" / "data" / "chunk-recovery.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(script: str):
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_reloads_once_per_page_then_falls_through():
    script = f"""
import {{ shouldReload }} from {json.dumps(MODULE.as_uri())};
const store = new Map();
const storage = {{ getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) }};
const first = shouldReload(storage, '/jobs/abc', 1000);
const second = shouldReload(storage, '/jobs/abc', 2000);
const other = shouldReload(storage, '/jobs/xyz', 3000);
const later = shouldReload(storage, '/jobs/abc', 1000 + 31000);
console.log(JSON.stringify([first, second, other, later]));
"""
    assert _run(script) == [True, False, True, True]


def test_recognises_a_chunk_load_failure_only():
    script = f"""
import {{ isChunkLoadError }} from {json.dumps(MODULE.as_uri())};
console.log(JSON.stringify([
  isChunkLoadError(new Error('Failed to fetch dynamically imported module: http://x/assets/Job-abc.js')),
  isChunkLoadError(new Error('Importing a module script failed.')),
  isChunkLoadError(new Error('Unable to preload CSS for /assets/Job.css')),
  isChunkLoadError(new Error('Network request failed: 500')),
  isChunkLoadError(null),
]));
"""
    assert _run(script) == [True, True, True, False, False]
