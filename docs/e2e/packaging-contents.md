# E2E evidence: what the built sdist and wheel actually contain

Worked proof for `cwcli-sdist-ships-tests`, taken against real `uv build` artifacts
rather than inferred from `MANIFEST.in`.

## Why this exists

The published `caffeinated-whale-cli` 1.1.0 sdist on PyPI contained **102 test files**.
The repository had no `MANIFEST.in`, so the sdist file list was whatever setuptools'
default produced, and that default sweeps `tests/` in.
Nobody chose to ship the suite; it happened by omission.

That is the route by which a private hostname committed to `tests/test_core_credbridge.py`
ended up inside an artifact anyone can download, which is a materially worse exposure
than a public git repository.

The general point outlives that one incident.
As long as the test suite ships in the distributed artifact, every fixture is public
distribution content, and fixtures are exactly where real hostnames, tenant names,
sample credentials and internal identifiers accumulate, because they feel like
throwaway scaffolding rather than published material.

The hostname incident itself is tracked separately as `cwcli-gov-hostname-in-tests`,
and the exposure of the already-published 1.1.0 artifact as `cwcli-1-1-0-artifact-exposure`.
Nothing already on PyPI was altered or unpublished here.

## The decision, stated in config

`MANIFEST.in` is a **deny-all allow-list**, not a deny-list:

```
global-exclude *
include pyproject.toml
include README.md
include LICENSE
recursive-include src/caffeinated_whale_cli *.py
recursive-include src/caffeinated_whale_cli *.html
```

A `prune tests` deny-list would only exclude the directories that existed when it was
written. With the allow-list, a newly added test, fixture or scaffolding directory is
out **by construction**.

## Proof

Ordered so the positive is asserted before the negative: a package that shipped nothing
would pass a test-file-absence check trivially.

### 1. The artifacts build

```
$ uv build
Successfully built dist/caffeinated_whale_cli-2.0.0.tar.gz
Successfully built dist/caffeinated_whale_cli-2.0.0-py3-none-any.whl
```

### 2. The sdist carries the build inputs and the whole package, and nothing else

Everything in the sdist outside `src/caffeinated_whale_cli/`:

```
caffeinated_whale_cli-2.0.0/LICENSE
caffeinated_whale_cli-2.0.0/PKG-INFO
caffeinated_whale_cli-2.0.0/README.md
caffeinated_whale_cli-2.0.0/pyproject.toml
caffeinated_whale_cli-2.0.0/setup.cfg
caffeinated_whale_cli-2.0.0/src/caffeinated_whale_cli.egg-info/SOURCES.txt
```

```
package modules in sdist: 80
test files in sdist:      0      (was 102 at 1.1.0)
```

`setup.cfg` and the `egg-info/` are written by setuptools into the release tree itself,
not drawn from the repository.

### 3. The wheel carries the package including its one data file

```
entries: 86
package modules: 80
console.html present: True
test files: 0
```

`commands/console.html` belongs to the retained, unregistered Console frontend.
It remains package data so the withheld implementation stays package-complete without exposing its command.

### 4. The tool genuinely installs and runs FROM THE SDIST

A clean `uv venv`, runtime dependencies only, installed from the `.tar.gz` (not the wheel),
with `CWCLI_HOME` pointed at a throwaway directory so the real `~/.cwcli` was untouched:

```
$ uv pip install dist/caffeinated_whale_cli-2.0.0.tar.gz
installed OK

$ cwcli --version
Caffeinated Whale CLI Version: 2.0.0 (source build, git unknown)

$ cwcli config path
/tmp/pf/home/config/config.toml

$ cwcli axi ls
instances[6]{projectName,status,ports}:
  ...

console.html readable from install: 63597 bytes
cwcli test files in site-packages: none
```

(`source build` is expected: installing from a local path records a PEP 610
`direct_url.json`. An index-resolved install reports `release build`.)

### 5. The sdist is a complete build input

A wheel rebuilt from the sdist has a file list identical to the wheel built directly
from the source tree, so the allow-list dropped nothing the build needs:

```
file list identical to the source-tree wheel: True (86 entries)
```

## Regression coverage

`tests/test_packaging_contents.py` runs in the fast `unit` tier, so the existing `Pytest`
CI check blocks a regression with no extra workflow step. It builds the real sdist and a
real source-tree wheel and asserts the positive before the negative.

Each assertion was falsified against a deliberately broken tree:

| Broken state | Result |
| --- | --- |
| `MANIFEST.in` removed (the literal 1.1.0 state) | FAIL - `assert ['tests/test_...'] == []`, "Left contains 102 more items" |
| A brand-new `qa/fixtures/conftest.py` holding a fake hostname | 0 files in the sdist, without `qa` being named anywhere - the allow-list working |
| A new `theme.json` added to `[tool.setuptools.package-data]` but not to `MANIFEST.in` | FAIL - "these package files are in the wheel but NOT in the sdist: ['commands/theme.json']" |

The third case is the allow-list's own failure mode - silently dropping a file the package
needs - guarded in the opposite direction from the leak.

## Known gap

The archive-content assertions are automated; the clean-venv **install and run** proof in
step 4 is worked evidence in this document, not a committed test. The nearest committed
cover is the `e2e_pkg` leg, which drives a full lifecycle against a runtime-deps-only
`uv tool install .` - built from the source tree, not from the sdist. Pointing that leg at
the sdist would close the gap and is not done here.
