"""``GET /api/v1/storage/logs`` and the three log-storage jobs on the web UI daemon."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from caffeinated_whale_cli import jobs
from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import accounts as core_accounts
from caffeinated_whale_cli.core import log_storage as ls
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

from .test_serve_frontend import _TIMEOUT, _TOKEN, _get, daemon  # noqa: F401 - fixture

_REPORT = ls.LogStorageReport(
    instances=[],
    journal=ls.JournalReport(
        True, None, "take up 8.0M", 8 * 1024**2, False, ["sudo journalctl --vacuum-size=500M"]
    ),
    docker_total=0,
    bench_total=0,
)


def test_storage_logs_serves_the_report_as_data(daemon, monkeypatch):  # noqa: F811
    seen = {}

    def fake(project=None):
        seen["project"] = project
        return Result(status=Status.OK, data=_REPORT)

    monkeypatch.setattr(serve_cmd.core_log_storage, "report", fake)
    status, body = _get(daemon.base + "/api/v1/storage/logs?project=p")
    assert status == 200 and seen["project"] == "p"
    assert body["data"]["journal"]["commands"] == ["sudo journalctl --vacuum-size=500M"]
    assert body["data"]["journal"]["size"] == 8 * 1024**2


def test_storage_logs_maps_a_core_error(daemon, monkeypatch):  # noqa: F811
    def boom(project=None):
        raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "nope")

    monkeypatch.setattr(serve_cmd.core_log_storage, "report", boom)
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(daemon.base + "/api/v1/storage/logs?project=x")
    assert error.value.code == 404


def test_storage_logs_is_same_origin_only(daemon, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        serve_cmd.core_log_storage,
        "report",
        lambda project=None: pytest.fail("cross-origin read reached the core"),
    )
    req = urllib.request.Request(
        daemon.base + "/api/v1/storage/logs",
        headers={
            "Authorization": f"Bearer {_TOKEN}",
            "Origin": "https://example.invalid",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310
    assert error.value.code == 403


@pytest.mark.parametrize(
    "verb", ["truncate-docker-logs", "set-docker-log-rotation", "prune-bench-logs"]
)
def test_the_log_storage_jobs_are_admin_jobs(verb):
    assert verb in jobs.ALLOWED_VERBS
    assert core_accounts.admin_only(verb) is True


def test_dispatch_routes_the_three_verbs_with_their_consent_and_args(monkeypatch):
    calls = []
    ok = Result(status=Status.OK, data={})
    monkeypatch.setattr(
        jobs.core_log_storage,
        "truncate_docker_logs",
        lambda p, *, consent: calls.append(("t", p, consent)) or ok,
    )
    monkeypatch.setattr(
        jobs.core_log_storage,
        "set_docker_log_rotation",
        lambda p, *, max_size, max_file: calls.append(("r", p, max_size, max_file)) or ok,
    )
    monkeypatch.setattr(
        jobs.core_log_storage,
        "prune_bench_logs",
        lambda p, *, bench, consent: calls.append(("p", p, bench, consent)) or ok,
    )
    rec = jobs._Recorder.__new__(jobs._Recorder)
    jobs._dispatch("truncate-docker-logs", {"project": "x"}, {}, True, {}, rec)
    jobs._dispatch(
        "set-docker-log-rotation",
        {"project": "x"},
        {"max_size": "20m", "max_file": "2"},
        False,
        {},
        rec,
    )
    jobs._dispatch("set-docker-log-rotation", {"project": "x"}, {}, False, {}, rec)
    jobs._dispatch("prune-bench-logs", {"project": "x", "bench": "1"}, {}, False, {}, rec)
    assert calls == [
        ("t", "x", True),
        ("r", "x", "20m", 2),
        ("r", "x", "10m", 3),
        ("p", "x", "1", False),
    ]
