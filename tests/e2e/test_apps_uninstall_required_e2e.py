"""Prove `cwcli apps uninstall` reports the truth when Frappe refuses an uninstall.

Frappe will not uninstall an app another installed app lists in its
`required_apps` hook, but `bench uninstall-app` only prints a warning and exits 0,
so cwcli used to report success while the app stayed installed.

Two throwaway apps are planted on the shared bench: a base app and a dependent
app whose `required_apps` names it. Both are installed on the session site through
`cwcli apps install` (which skips the fetch for an app already under `apps/`), and
their presence is read back from Frappe before any uninstall, so the absence
checks cannot pass vacuously:

- uninstalling the base app alone exits non-zero, names the dependent app, and
  leaves both apps installed;
- uninstalling the dependent app first and the base app second in one call
  removes both and exits 0.

The apps never come from a network fetch or a pip build: each is a minimal
package on the bench's path via a `.pth` file, registered in `sites/apps.txt` on
its own line. A never-raising finalizer removes them from the site, `apps.txt`, the
venv path, and the bench, so a later test sees the bench it expects.

Commit to run on CI (real Docker); do NOT run locally.
"""

from __future__ import annotations

import json
import shlex
import textwrap

import pytest

from . import harness
from .test_start_status_e2e import _ensure_serving, _wait_supervised_stack

pytestmark = pytest.mark.e2e

_BASE = "cwe2e_reqbase"
_DEP = "cwe2e_reqdep"
_PTH = "cwe2e_required_apps.pth"


def _sh(inst, script: str) -> str:
    code, out = harness.exec_in_frappe(inst.name, script)
    assert code == 0, f"{script}\n{out}"
    return out


def _installed_on_site(inst) -> list[str]:
    """Authoritative site state: the apps `frappe.get_installed_apps` reports."""
    out = _sh(
        inst, f"cd {inst.bench} && bench --site {inst.site} execute frappe.get_installed_apps"
    )
    return json.loads(out.strip().splitlines()[-1])


def _app_script(app: str, required: list[str]) -> str:
    """Shell that writes a minimal installable Frappe app at `apps/<app>`."""
    hooks = (
        f'app_name = "{app}"\n'
        f'app_title = "{app}"\n'
        'app_publisher = "cwe2e"\n'
        'app_description = "cwcli E2E required-apps probe"\n'
        'app_email = "cwe2e@example.com"\n'
        'app_license = "mit"\n'
        f"required_apps = {required!r}\n"
    )
    pyproject = f'[project]\nname = "{app}"\nversion = "0.0.1"\n'
    return textwrap.dedent(
        f"""
        A=apps/{app}
        rm -rf "$A"
        mkdir -p "$A/{app}"
        printf '%s' {shlex.quote(hooks)} > "$A/{app}/hooks.py"
        printf '__version__ = "0.0.1"\\n' > "$A/{app}/__init__.py"
        : > "$A/{app}/modules.txt"
        : > "$A/{app}/patches.txt"
        printf '%s' {shlex.quote(pyproject)} > "$A/pyproject.toml"
        git -C "$A" init -q -b main
        git -C "$A" add -A
        git -C "$A" -c user.email=e2e@example.com -c user.name=e2e -c commit.gpgsign=false \\
            commit -qm init
        echo "$PWD/$A" >> "$PURELIB/{_PTH}"
        grep -qx {app} sites/apps.txt || {{
            if [ -s sites/apps.txt ] && [ -n "$(tail -c1 sites/apps.txt)" ]; then
                echo >> sites/apps.txt
            fi
            printf '%s\\n' {app} >> sites/apps.txt
        }}
        """
    )


def _cleanup(inst) -> None:
    """Remove both apps from the site and the bench. Never raises."""
    script = textwrap.dedent(
        f"""
        cd {inst.bench}
        for a in {_DEP} {_BASE}; do
            bench --site {inst.site} uninstall-app "$a" --yes --no-backup --force \\
                >/dev/null 2>&1 || true
            bench --site {inst.site} remove-from-installed-apps "$a" >/dev/null 2>&1 || true
        done
        for a in {_DEP} {_BASE}; do
            sed -i "/^$a\\$/d" sites/apps.txt 2>/dev/null || true
            rm -rf "apps/$a"
        done
        PURELIB=$(env/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
        rm -f "$PURELIB/{_PTH}"
        bench --site {inst.site} clear-cache >/dev/null 2>&1 || true
        """
    )
    try:
        harness.exec_in_frappe(inst.name, script)
    except Exception:  # noqa: BLE001 - a finalizer must never mask the real failure
        pass


@pytest.fixture()
def required_pair(running_instance):
    """The base and dependent apps, planted on the bench and installed on the site."""
    inst = running_instance
    _ensure_serving(inst.name)
    _wait_supervised_stack(inst.name)
    _cleanup(inst)  # belt-and-suspenders: clear any prior remnant
    try:
        _sh(
            inst,
            "set -e\n"
            f"cd {inst.bench}\n"
            "PURELIB=$(env/bin/python -c "
            "'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])')\n"
            + _app_script(_BASE, [])
            + _app_script(_DEP, [_BASE]),
        )
        with harness.quiesce_v14_asset_watcher(inst.name, inst.bench):
            install = harness.run_cwcli(
                "apps",
                "install",
                inst.name,
                "--app",
                _BASE,
                "--app",
                _DEP,
                "--site",
                inst.site,
                "--yes",
            )
        assert install.returncode == 0, install.stdout + install.stderr
        installed = _installed_on_site(inst)
        assert _BASE in installed and _DEP in installed, installed
        yield inst
    finally:
        _cleanup(inst)
        _ensure_serving(inst.name)


def test_uninstalling_a_required_app_is_refused_and_names_the_dependent(required_pair):
    inst = required_pair

    result = harness.run_cwcli(
        "apps", "uninstall", inst.name, "--app", _BASE, "--site", inst.site, "--yes"
    )
    text = harness.collapse_ws(harness.strip_ansi(result.stdout + result.stderr))

    assert result.returncode != 0, text
    assert f"is required by {_DEP}" in text, text
    installed = _installed_on_site(inst)
    assert _BASE in installed, "the required app was removed"
    assert _DEP in installed, "the dependent app was removed"


def test_uninstalling_the_dependent_first_removes_both(required_pair):
    inst = required_pair

    result = harness.run_cwcli(
        "apps",
        "uninstall",
        inst.name,
        "--app",
        _DEP,
        "--app",
        _BASE,
        "--site",
        inst.site,
        "--yes",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    installed = _installed_on_site(inst)
    assert _DEP not in installed, "the dependent app is still installed"
    assert _BASE not in installed, "the required app is still installed"
