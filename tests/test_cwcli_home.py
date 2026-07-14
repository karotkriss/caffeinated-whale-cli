"""Mock-free tests for the CWCLI_HOME override.

These tests set a REAL ``CWCLI_HOME`` environment variable and inspect REAL
filesystem results - there is no Mock/patch of cwcli's own behavior.

cwcli's footprint paths (``CONFIG_DIR`` / ``PROJECTS_DIR`` in ``config_utils``,
``CACHE_DIR`` / ``DB_PATH`` in ``db_utils``, ``PID_DIR`` in ``auto_inspect``)
are resolved at import time via the shared ``config_utils.cwcli_home()``
helper. So the module-constant and permission cases run a fresh subprocess with
the env set - exactly how a real ``cwcli`` process resolves them - while the
pure ``cwcli_home()`` logic is exercised directly.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from caffeinated_whale_cli.utils import config_utils

# A probe that imports cwcli fresh, initializes the cache DB (so the 0600 file
# permission is applied), and prints the resolved footprint on a marker line.
_PROBE = r"""
import json, os, stat
from caffeinated_whale_cli.utils import config_utils, db_utils, auto_inspect

db_utils.initialize_database()  # creates the cache DB and chmods it to 0600

out = {
    "base": str(config_utils.cwcli_home()),
    "config_dir": str(config_utils.CONFIG_DIR),
    "projects_dir": str(config_utils.PROJECTS_DIR),
    "cache_dir": str(db_utils.CACHE_DIR),
    "db_path": str(db_utils.DB_PATH),
    "pid_dir": str(auto_inspect.PID_DIR),
    "cache_dir_mode": oct(stat.S_IMODE(os.stat(db_utils.CACHE_DIR).st_mode)),
    "db_file_mode": oct(stat.S_IMODE(os.stat(db_utils.DB_PATH).st_mode)),
}
print("CWCLIHOME_RESULT:" + json.dumps(out))
"""


def _resolve_in_subprocess(env_overrides: dict) -> dict:
    """Import cwcli in a fresh process with the given env and return its paths.

    A value of ``None`` in ``env_overrides`` removes that variable (to test a
    genuinely-unset ``CWCLI_HOME``).
    """
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("CWCLIHOME_RESULT:"):
            return json.loads(line[len("CWCLIHOME_RESULT:") :])
    raise AssertionError(f"probe produced no result line:\n{result.stdout}\n{result.stderr}")


class TestCwcliHomeHelper:
    """The pure cwcli_home() logic, called directly with a real env var."""

    def test_unset_falls_back_to_dot_cwcli(self, monkeypatch):
        monkeypatch.delenv("CWCLI_HOME", raising=False)
        assert config_utils.cwcli_home() == Path.home() / ".cwcli"

    def test_empty_value_falls_back_to_dot_cwcli(self, monkeypatch):
        # An empty string is falsy, so it behaves like unset (not a "" base).
        monkeypatch.setenv("CWCLI_HOME", "")
        assert config_utils.cwcli_home() == Path.home() / ".cwcli"

    def test_set_redirects_base(self, monkeypatch, tmp_path):
        """CWCLI_HOME redirects the base directory `cwcli_home()` returns."""
        target = tmp_path / "cwe2e-home"
        monkeypatch.setenv("CWCLI_HOME", str(target))
        assert config_utils.cwcli_home() == target


class TestCwcliHomeRelocation:
    """The import-time module constants, resolved in a real subprocess."""

    def test_set_redirects_all_footprint_paths(self, tmp_path):
        target = tmp_path / "cwe2e-home"
        resolved = _resolve_in_subprocess({"CWCLI_HOME": str(target)})

        assert resolved["base"] == str(target)
        assert resolved["config_dir"] == str(target / "config")
        assert resolved["projects_dir"] == str(target / "projects")
        assert resolved["cache_dir"] == str(target / "cache")
        assert resolved["db_path"] == str(target / "cache" / "cwc-cache.db")
        assert resolved["pid_dir"] == str(target / "run")
        # The override must move the WHOLE footprint, never leaving anything
        # under the real ~/.cwcli.
        assert ".cwcli" not in resolved["base"]

    def test_unset_keeps_default_home_location(self, tmp_path):
        # Point HOME at a throwaway dir and leave CWCLI_HOME unset, proving the
        # default resolves to <HOME>/.cwcli (and never the real ~/.cwcli).
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        resolved = _resolve_in_subprocess({"HOME": str(fake_home), "CWCLI_HOME": None})

        assert resolved["base"] == str(fake_home / ".cwcli")
        assert resolved["config_dir"] == str(fake_home / ".cwcli" / "config")
        assert resolved["projects_dir"] == str(fake_home / ".cwcli" / "projects")
        assert resolved["cache_dir"] == str(fake_home / ".cwcli" / "cache")
        assert resolved["pid_dir"] == str(fake_home / ".cwcli" / "run")

    @pytest.mark.skipif(os.name == "nt", reason="Unix permissions not supported on Windows")
    def test_relocated_cache_keeps_secure_permissions(self, tmp_path):
        target = tmp_path / "cwe2e-home"
        resolved = _resolve_in_subprocess({"CWCLI_HOME": str(target)})

        # Same hardening as the default location: 0700 cache dir, 0600 DB file.
        assert resolved["cache_dir_mode"] == oct(0o700)
        assert resolved["db_file_mode"] == oct(0o600)

    def test_single_helper_agreement(self, tmp_path):
        """Every footprint dir resolves under the same CWCLI_HOME base."""
        # config_utils, db_utils, and auto_inspect all resolve under the SAME
        # base when CWCLI_HOME is set, because they share the one cwcli_home()
        # helper - they can never diverge on where cwcli's state lives.
        target = tmp_path / "cwe2e-home"
        resolved = _resolve_in_subprocess({"CWCLI_HOME": str(target)})

        config_base = str(Path(resolved["config_dir"]).parent)
        projects_base = str(Path(resolved["projects_dir"]).parent)
        cache_base = str(Path(resolved["cache_dir"]).parent)
        pid_base = str(Path(resolved["pid_dir"]).parent)

        assert config_base == projects_base == cache_base == pid_base == resolved["base"]
