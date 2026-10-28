"""The fix claim on every advisory row is honest and specific (steering 035): the
newest release on the TRACKED branch is read from the app's own remote, a
dependency row names the pin that controls it, and a commit-only fix is named by
version. Nothing here touches the network: refs and raw files are stubbed."""

from __future__ import annotations

from caffeinated_whale_cli.core import advisories as adv
from caffeinated_whale_cli.core.git_refs import RemoteRefs

FRAPPE = "https://github.com/frappe/frappe.git"
PYPROJECT_15 = b'[project]\ndependencies = [\n    "Pillow~=12.3.0",\n    "GitPython~=3.1.34",\n]\n'
PYPROJECT_16 = b'[project]\ndependencies = [\n    "Pillow~=13.0.0",\n]\n'


class _Fetcher:
    def __init__(self, files):
        self.files = files

    def get(self, url, *, headers=None, timeout=0):
        body = self.files.get(url)
        return (200, body, True) if body is not None else (404, None, False)


def _inv(branch="version-15", version="15.116.1", python=None, remote=FRAPPE):
    return {
        "projects": {
            "p": {
                "benches": {
                    "0": {
                        "apps": {
                            "frappe": {"version": version, "branch": branch, "remote": remote}
                        },
                        "python": python or {},
                        "node": {},
                    }
                },
                "images": {},
            }
        }
    }


def _aset(adv_id, ranges, matches):
    aset = adv.AdvisorySet()
    aset.by_id[adv_id] = {"id": adv_id, "ranges": ranges, "matches": matches}
    return aset


def _match(kind, name, installed, fixed, **extra):
    return {
        "project": "p",
        "bench_index": "0",
        "kind": kind,
        "name": name,
        "installed": installed,
        "fixed": fixed,
        "status": "affected",
        "action": {"verb": "apps-update"},
        **extra,
    }


def _stub_tags(monkeypatch, tags, ok=True):
    monkeypatch.setattr(
        adv.git_refs,
        "list_remote_refs",
        lambda url: RemoteRefs(url=url, tags=tags, ok=ok, error=None if ok else "down"),
    )


def test_an_app_row_names_the_newest_release_on_the_tracked_branch(monkeypatch):
    _stub_tags(monkeypatch, ["v16.40.0", "v15.125.0", "v15.124.0"])
    rng = [{"source": "github", "target_name": "frappe", "fixed": "15.121.0"}]
    aset = _aset("GHSA-1", rng, [_match("app", "frappe", "15.116.1", "15.121.0")])
    adv.fix_paths(aset, _inv(), _Fetcher({}), {})
    m = aset.by_id["GHSA-1"]["matches"][0]
    assert m["fix"]["kind"] == "ready"
    assert m["fix"]["text"] == "Update frappe to 15.125.0 fixes this"
    assert m["action"] == {"verb": "apps-update", "apps": "frappe", "project": "p", "bench": "0"}


def test_a_fix_not_on_the_tracked_branch_is_said_so_without_an_action(monkeypatch):
    _stub_tags(monkeypatch, ["v16.40.0", "v15.120.0"])
    # newest 15.x is older than the fix
    aset = _aset("GHSA-2", [], [_match("app", "frappe", "15.116.1", "15.121.0")])
    adv.fix_paths(aset, _inv(), _Fetcher({}), {})
    m = aset.by_id["GHSA-2"]["matches"][0]
    assert m["fix"]["kind"] == "notyet" and "15.120.0" in m["fix"]["text"]
    assert m["action"] is None
    # the fix is on another major
    aset = _aset("GHSA-3", [], [_match("app", "frappe", "15.116.1", "16.34.0")])
    adv.fix_paths(aset, _inv(), _Fetcher({}), {})
    m = aset.by_id["GHSA-3"]["matches"][0]
    assert m["fix"]["kind"] == "other_major" and "tracks version-15" in m["fix"]["text"]
    assert m["action"] is None
    # develop is not a release branch
    aset = _aset("GHSA-4", [], [_match("app", "frappe", "15.116.1", "15.121.0")])
    adv.fix_paths(aset, _inv(branch="develop"), _Fetcher({}), {})
    assert aset.by_id["GHSA-4"]["matches"][0]["fix"]["kind"] == "not_release_branch"
    # a fork whose remote lacks the tag
    _stub_tags(monkeypatch, ["v15.118.0"])
    aset = _aset("GHSA-5", [], [_match("app", "frappe", "15.116.1", "15.121.0")])
    adv.fix_paths(aset, _inv(remote="https://github.com/acme/frappe.git"), _Fetcher({}), {})
    m = aset.by_id["GHSA-5"]["matches"][0]
    assert m["fix"]["kind"] == "notyet" and "fork has not pulled it" in m["fix"]["text"]
    assert m["action"] is None


def test_a_commit_only_fix_is_named_by_version_from_the_affected_tag_list(monkeypatch):
    _stub_tags(monkeypatch, ["v15.125.0"])
    sha = "9523516cac25992bc2cd810e1015df8994c257f5"
    rng = [
        {
            "source": "osv",
            "target_kind": "git",
            "target_name": "https://github.com/frappe/frappe",
            "fixed": sha,
            "affected_versions": ["v15.0.0", "v15.116.1", "v15.120.0"],
        }
    ]
    aset = _aset("CVE-1", rng, [_match("app", "frappe", "15.116.1", sha)])
    adv.fix_paths(aset, _inv(), _Fetcher({}), {})
    m = aset.by_id["CVE-1"]["matches"][0]
    assert m["fix"]["kind"] == "ready"
    assert m["fix"]["fixed_label"] == "a commit after 15.116.1"
    assert m["fix"]["text"] == "fixed in a commit after 15.116.1; update frappe to 15.125.0"
    assert sha[:7] not in m["fix"]["short"]
    # the newest release is itself still listed as affected: no action
    rng[0]["affected_versions"].append("v15.125.0")
    aset = _aset("CVE-2", rng, [_match("app", "frappe", "15.116.1", sha)])
    adv.fix_paths(aset, _inv(), _Fetcher({}), {})
    m = aset.by_id["CVE-2"]["matches"][0]
    assert m["fix"]["kind"] == "notyet" and m["action"] is None


def test_a_dependency_row_names_the_pin_that_controls_it(monkeypatch):
    _stub_tags(monkeypatch, ["v15.125.0"])
    raw = "https://raw.githubusercontent.com/frappe/frappe/version-15/pyproject.toml"
    fetcher = _Fetcher({raw: PYPROJECT_15})
    inv = _inv(python={"pillow": "12.3.0", "gitpython": "3.1.62", "idna": "3.4"})
    aset = _aset(
        "GHSA-P",
        [],
        [
            _match("python-package", "pillow", "12.3.0", "13.0.0"),
            _match("python-package", "gitpython", "3.1.62", "3.2.0"),
            _match("python-package", "idna", "3.4", "3.7"),
        ],
    )
    cache = adv.fix_paths(aset, inv, fetcher, {})
    pillow, gitpython, idna = aset.by_id["GHSA-P"]["matches"]
    assert pillow["fix"]["kind"] == "pinned" and "Pillow~=12.3.0" in pillow["fix"]["text"]
    assert "does not bring 13.0.0" in pillow["fix"]["text"] and pillow["action"] is None
    assert gitpython["fix"]["kind"] == "pinned" and gitpython["action"] is None
    assert idna["fix"]["kind"] == "transitive" and idna["action"] is None
    assert cache["pins"][raw] == {"pillow": "Pillow~=12.3.0", "gitpython": "GitPython~=3.1.34"}


def test_a_dependency_the_tip_pins_past_the_fix_offers_the_app_update(monkeypatch):
    _stub_tags(monkeypatch, ["v16.52.0"])
    raw = "https://raw.githubusercontent.com/frappe/frappe/version-16/pyproject.toml"
    inv = _inv(branch="version-16", version="16.50.0", python={"pillow": "12.3.0"})
    aset = _aset("GHSA-Q", [], [_match("python-package", "pillow", "12.3.0", "13.0.0")])
    adv.fix_paths(aset, inv, _Fetcher({raw: PYPROJECT_16}), {})
    m = aset.by_id["GHSA-Q"]["matches"][0]
    assert m["fix"]["kind"] == "ready" and "brings 13.0.0" in m["fix"]["text"]
    assert m["action"]["apps"] == "frappe"


def test_an_unreadable_remote_or_pin_is_reported_not_guessed(monkeypatch):
    _stub_tags(monkeypatch, [], ok=False)
    aset = _aset("GHSA-U", [], [_match("app", "frappe", "15.116.1", "15.121.0")])
    adv.fix_paths(aset, _inv(), _Fetcher({}), {})
    m = aset.by_id["GHSA-U"]["matches"][0]
    assert m["fix"]["kind"] == "unchecked" and m["action"] is None

    class Down(_Fetcher):
        def get(self, url, *, headers=None, timeout=0):
            return (0, None, False)

    aset = _aset("GHSA-V", [], [_match("python-package", "pillow", "12.3.0", "13.0.0")])
    adv.fix_paths(aset, _inv(python={"pillow": "12.3.0"}), Down({}), {})
    m = aset.by_id["GHSA-V"]["matches"][0]
    assert m["fix"]["kind"] == "unchecked" and m["action"] is None
