"""``core.version`` - install-method detection, PyPI latest-lookup, PEP 440
compare (incl. the dev-ahead case), the TTL cache, and the fail-open contract.

UI-pure: the module prints/exits nothing, so every case asserts on the returned
:class:`VersionInfo` / values. The network and ``importlib.metadata`` are faked;
no real PyPI request is made and the cache is redirected via ``CWCLI_HOME``.
"""

import json
import time

import pytest

from caffeinated_whale_cli.core import version as core_version
from caffeinated_whale_cli.core.envelope import Status


class FakeDist:
    """A stand-in for ``importlib.metadata.distribution`` return value."""

    def __init__(self, direct_url=None, location="/opt/venv/lib/python3.13/site-packages"):
        self._direct_url = direct_url
        self._location = location

    def read_text(self, name):
        return self._direct_url if name == "direct_url.json" else None

    def locate_file(self, path):
        return self._location


def _patch_dist(monkeypatch, dist):
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: dist)


class FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, *, version=None, raises=None, body=None):
    def fake_urlopen(req, timeout=None):
        if raises is not None:
            raise raises
        payload = body if body is not None else json.dumps({"info": {"version": version}}).encode()
        return FakeResp(payload)

    monkeypatch.setattr(core_version.urllib.request, "urlopen", fake_urlopen)


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the version cache under a throwaway CWCLI_HOME."""
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path))
    return tmp_path


class TestDetectMethod:
    def test_editable_checkout_is_dev(self, monkeypatch):
        du = json.dumps({"url": "file:///home/me/cwcli", "dir_info": {"editable": True}})
        _patch_dist(monkeypatch, FakeDist(direct_url=du))
        method, path = core_version._detect_method()
        assert method == "dev"
        assert path == "/home/me/cwcli"

    def test_uv_tool_path(self, monkeypatch):
        loc = "/home/me/.local/share/uv/tools/caffeinated-whale-cli/lib/python3.13/site-packages"
        _patch_dist(monkeypatch, FakeDist(location=loc))
        assert core_version._detect_method() == ("uv", None)

    def test_uvx_ephemeral_path(self, monkeypatch):
        loc = "/home/me/.cache/uv/environments-v2/abcd/lib/python3.13/site-packages"
        _patch_dist(monkeypatch, FakeDist(location=loc))
        assert core_version._detect_method() == ("uvx", None)

    def test_pip_fallback(self, monkeypatch):
        _patch_dist(monkeypatch, FakeDist(location="/usr/lib/python3.13/site-packages"))
        assert core_version._detect_method() == ("pip", None)

    def test_non_editable_direct_url_falls_through_to_path(self, monkeypatch):
        # A VCS/archive install has direct_url.json but no dir_info.editable.
        du = json.dumps({"url": "https://x/y.whl", "archive_info": {}})
        _patch_dist(monkeypatch, FakeDist(direct_url=du, location="/usr/lib/site-packages"))
        assert core_version._detect_method() == ("pip", None)

    def test_distribution_error_defaults_to_pip(self, monkeypatch):
        import importlib.metadata

        def boom(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "distribution", boom)
        assert core_version._detect_method() == ("pip", None)


class TestUpgradeCommand:
    def test_uv(self):
        assert core_version._upgrade_command("uv") == [
            "uv",
            "tool",
            "upgrade",
            "caffeinated-whale-cli",
        ]

    def test_pip_uses_sys_executable(self):
        import sys

        cmd = core_version._upgrade_command("pip")
        assert cmd == [sys.executable, "-m", "pip", "install", "--upgrade", "caffeinated-whale-cli"]

    def test_dev_and_uvx_have_no_command(self):
        assert core_version._upgrade_command("dev") is None
        assert core_version._upgrade_command("uvx") is None


class TestIsOutdated:
    def test_older_current_is_outdated(self):
        assert core_version._is_outdated("0.35.0", "0.37.0") is True

    def test_dev_ahead_is_not_outdated(self):
        # Local 0.37.0 is newer than published 0.35.0 - must read as up to date.
        assert core_version._is_outdated("0.37.0", "0.35.0") is False

    def test_equal_is_not_outdated(self):
        assert core_version._is_outdated("0.37.0", "0.37.0") is False

    def test_pep440_not_string_compare(self):
        # String compare would call "0.9.0" > "0.10.0"; PEP 440 must not.
        assert core_version._is_outdated("0.9.0", "0.10.0") is True

    def test_none_latest_is_not_outdated(self):
        assert core_version._is_outdated("0.37.0", None) is False

    def test_invalid_version_is_not_outdated(self):
        assert core_version._is_outdated("0.37.0", "not-a-version") is False


class TestLatestVersionAndCache:
    def test_fetch_from_pypi(self, isolated_cache, monkeypatch):
        _patch_urlopen(monkeypatch, version="0.40.0")
        assert core_version._latest_version(use_cache=False, timeout=1.0) == "0.40.0"

    def test_fetch_writes_cache(self, isolated_cache, monkeypatch):
        _patch_urlopen(monkeypatch, version="0.40.0")
        core_version._latest_version(use_cache=False, timeout=1.0)
        cached = json.loads((isolated_cache / "cache" / "version_check.json").read_text())
        assert cached["latest"] == "0.40.0"

    def test_cache_hit_skips_network(self, isolated_cache, monkeypatch):
        cache = isolated_cache / "cache" / "version_check.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"latest": "0.41.0", "checked_at": time.time()}))

        def explode(req, timeout=None):
            raise AssertionError("network must not be hit on a fresh cache")

        monkeypatch.setattr(core_version.urllib.request, "urlopen", explode)
        assert core_version._latest_version(use_cache=True, timeout=1.0) == "0.41.0"

    def test_expired_cache_refetches(self, isolated_cache, monkeypatch):
        cache = isolated_cache / "cache" / "version_check.json"
        cache.parent.mkdir(parents=True)
        stale = time.time() - core_version._CACHE_TTL_SECONDS - 10
        cache.write_text(json.dumps({"latest": "0.41.0", "checked_at": stale}))
        _patch_urlopen(monkeypatch, version="0.42.0")
        assert core_version._latest_version(use_cache=True, timeout=1.0) == "0.42.0"

    def test_no_cache_ignores_fresh_cache(self, isolated_cache, monkeypatch):
        cache = isolated_cache / "cache" / "version_check.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"latest": "0.41.0", "checked_at": time.time()}))
        _patch_urlopen(monkeypatch, version="0.99.0")
        assert core_version._latest_version(use_cache=False, timeout=1.0) == "0.99.0"


class TestFailOpen:
    def test_network_error_returns_none(self, isolated_cache, monkeypatch):
        import urllib.error

        _patch_urlopen(monkeypatch, raises=urllib.error.URLError("no route"))
        assert core_version._fetch_latest(1.0) is None

    def test_bad_json_returns_none(self, isolated_cache, monkeypatch):
        _patch_urlopen(monkeypatch, body=b"not json at all")
        assert core_version._fetch_latest(1.0) is None

    def test_missing_key_returns_none(self, isolated_cache, monkeypatch):
        _patch_urlopen(monkeypatch, body=json.dumps({"info": {}}).encode())
        assert core_version._fetch_latest(1.0) is None


class TestCheck:
    def _patch(self, monkeypatch, *, method, current="0.35.0", latest="0.37.0", dev_path=None):
        monkeypatch.setattr(core_version, "_current_version", lambda: current)
        monkeypatch.setattr(core_version, "_detect_method", lambda: (method, dev_path))
        monkeypatch.setattr(core_version, "_latest_version", lambda *, use_cache, timeout: latest)

    def test_outdated_uv_install(self, monkeypatch):
        self._patch(monkeypatch, method="uv")
        result = core_version.check()
        assert result.status is Status.OK
        info = result.data
        assert info.method == "uv"
        assert info.is_outdated is True
        assert info.is_dev is False
        assert info.upgrade_command == ["uv", "tool", "upgrade", "caffeinated-whale-cli"]

    def test_dev_checkout(self, monkeypatch):
        self._patch(monkeypatch, method="dev", dev_path="/src/cwcli", latest="0.37.0")
        info = core_version.check().data
        assert info.is_dev is True
        assert info.dev_path == "/src/cwcli"
        assert info.upgrade_command is None

    def test_dev_ahead_reads_up_to_date(self, monkeypatch):
        self._patch(monkeypatch, method="pip", current="0.37.0", latest="0.35.0")
        info = core_version.check().data
        assert info.is_outdated is False

    def test_network_failure_warns_and_nulls_latest(self, monkeypatch):
        self._patch(monkeypatch, method="pip", latest=None)
        result = core_version.check()
        assert result.status is Status.WARNING
        assert result.data.latest is None
        assert result.data.is_outdated is False
        assert result.warnings[0].code == "pypi.unreachable"


class TestPassiveNotice:
    """The cache-only, non-blocking gate behind the passive 'update' notice."""

    def _patch(self, monkeypatch, *, method="uv", current="0.35.0"):
        monkeypatch.setattr(core_version, "_detect_method", lambda: (method, None))
        monkeypatch.setattr(core_version, "_current_version", lambda: current)
        spawned = []
        monkeypatch.setattr(
            core_version, "_spawn_background_refresh", lambda **kw: spawned.append(kw)
        )
        # A fresh cache read must NEVER trigger a network call on the hot path.
        monkeypatch.setattr(
            core_version.urllib.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("passive notice must not hit net")),
        )
        return spawned

    def test_outdated_from_fresh_cache_notifies_without_network_or_spawn(
        self, isolated_cache, monkeypatch
    ):
        spawned = self._patch(monkeypatch, method="uv", current="0.35.0")
        monkeypatch.setattr(core_version, "_read_cache", lambda: "0.37.0")
        info = core_version.passive_notice()
        assert info is not None
        assert info.is_outdated is True
        assert info.current == "0.35.0" and info.latest == "0.37.0"
        assert info.upgrade_command == ["uv", "tool", "upgrade", "caffeinated-whale-cli"]
        assert spawned == []  # fresh cache -> no background refresh

    def test_up_to_date_shows_nothing(self, monkeypatch):
        self._patch(monkeypatch, current="0.37.0")
        monkeypatch.setattr(core_version, "_read_cache", lambda: "0.37.0")
        assert core_version.passive_notice() is None

    def test_missing_cache_returns_none_and_spawns_refresh(self, monkeypatch):
        spawned = self._patch(monkeypatch)
        monkeypatch.setattr(core_version, "_read_cache", lambda: None)
        assert core_version.passive_notice() is None
        assert len(spawned) == 1  # kicked a detached refresh for next time

    def test_dev_checkout_never_fetches_or_spawns(self, monkeypatch):
        spawned = self._patch(monkeypatch, method="dev")
        monkeypatch.setattr(
            core_version, "_read_cache", lambda: (_ for _ in ()).throw(AssertionError("no read"))
        )
        assert core_version.passive_notice() is None
        assert spawned == []

    def test_uvx_never_fetches_or_spawns(self, monkeypatch):
        spawned = self._patch(monkeypatch, method="uvx")
        monkeypatch.setattr(
            core_version, "_read_cache", lambda: (_ for _ in ()).throw(AssertionError("no read"))
        )
        assert core_version.passive_notice() is None
        assert spawned == []

    def test_fail_open_on_error(self, monkeypatch):
        self._patch(monkeypatch)
        monkeypatch.setattr(
            core_version, "_read_cache", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert core_version.passive_notice() is None  # swallowed -> no notice
