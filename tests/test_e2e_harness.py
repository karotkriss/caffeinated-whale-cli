"""Pure tests for E2E harness behavior that does not require Docker."""

from __future__ import annotations

import pytest

from .e2e import harness


def test_v14_asset_watcher_is_restored_when_the_protected_build_raises(monkeypatch):
    calls = []

    def exec_recording(project, script, workdir=None):
        calls.append((project, script))
        return 0, ""

    monkeypatch.setattr(harness, "FRAPPE_MAJOR", 14)
    monkeypatch.setattr(harness, "exec_in_frappe", exec_recording)

    with pytest.raises(RuntimeError, match="build failed"):
        with harness.quiesce_v14_asset_watcher("cwe2e-test", "/workspace/bench") as changed:
            assert changed is True
            raise RuntimeError("build failed")

    assert [script.rsplit(" ", 2)[-2:] for _project, script in calls] == [
        ["stop", "watch"],
        ["start", "watch"],
    ]


def test_newer_frappe_asset_builds_do_not_touch_the_watcher(monkeypatch):
    calls = []
    monkeypatch.setattr(harness, "FRAPPE_MAJOR", 15)
    monkeypatch.setattr(
        harness,
        "exec_in_frappe",
        lambda *args, **kwargs: calls.append((args, kwargs)) or (0, ""),
    )

    with harness.quiesce_v14_asset_watcher("cwe2e-test") as changed:
        assert changed is False

    assert calls == []
