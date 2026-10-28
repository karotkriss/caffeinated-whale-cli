"""A job's detail opens with a bounded tail of its events.

The throwaway init job wrote 4,318 events; ``GET /api/v1/jobs/<id>?after=0``
answered 20 MB and the page took seconds to render, which read as "the job will
not open". ``read_job(limit=N)`` returns the last N events and says how many
earlier ones were left out; the page asks for 400 first and the rest on demand.
"""

import json

from caffeinated_whale_cli import jobs as core_jobs


def _write_job(job_id: str, count: int) -> None:
    spec = core_jobs._json_path(job_id, "spec.json")
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(json.dumps({"id": job_id, "verb": "backup", "target": {"project": "p"}, "args": {}}))
    core_jobs._json_path(job_id, "state.json").write_text(
        json.dumps({"id": job_id, "state": "succeeded", "created_epoch": 0})
    )
    lines = [json.dumps({"seq": i, "type": "x_output", "message": f"line {i}"}) for i in range(1, count + 1)]
    core_jobs._json_path(job_id, "events.jsonl").write_text("\n".join(lines) + "\n")


def test_limit_returns_the_tail_and_counts_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path))
    _write_job("20260101000000-limit", 1000)
    record = core_jobs.read_job("20260101000000-limit", include_events=True, limit=400)
    assert len(record["events"]) == 400
    assert record["events"][0]["seq"] == 601
    assert record["events"][-1]["seq"] == 1000
    assert record["events_skipped"] == 600


def test_no_limit_returns_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path))
    _write_job("20260101000000-all", 50)
    record = core_jobs.read_job("20260101000000-all", include_events=True)
    assert len(record["events"]) == 50
    assert record["events_skipped"] == 0


def test_resume_after_a_cursor_is_unaffected_by_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path))
    _write_job("20260101000000-resume", 30)
    record = core_jobs.read_job("20260101000000-resume", include_events=True, after=25, limit=400)
    assert [e["seq"] for e in record["events"]] == [26, 27, 28, 29, 30]
    assert record["events_skipped"] == 0
