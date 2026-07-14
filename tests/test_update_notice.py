"""The passive update notice frontend: stderr-only, TTY-gated, env-suppressible.

Proves the hard contract: the notice NEVER lands on stdout (the axi TOON path
above all), only shows to an interactive human, and is fully fail-open. The core
gate (``core.version.passive_notice``) is faked here - this file tests only the
rendering/gating frontend.
"""

from caffeinated_whale_cli import update_notice
from caffeinated_whale_cli.core.version import VersionInfo

_OUTDATED = VersionInfo(
    current="0.35.0",
    latest="0.37.0",
    method="uv",
    upgrade_command=["uv", "tool", "upgrade", "caffeinated-whale-cli"],
    is_outdated=True,
    is_dev=False,
)


def _fake_notice(info):
    """Patch the core gate to return ``info`` and record that it was consulted."""
    calls = []

    def fake():
        calls.append(True)
        return info

    return fake, calls


def test_notice_goes_to_stderr_never_stdout(monkeypatch, capsys):
    monkeypatch.delenv(update_notice._ENV_DISABLE, raising=False)
    monkeypatch.setattr(update_notice, "_stderr_is_tty", lambda: True)
    fake, _ = _fake_notice(_OUTDATED)
    monkeypatch.setattr("caffeinated_whale_cli.core.version.passive_notice", fake)

    update_notice.notify_if_outdated()

    out, err = capsys.readouterr()
    assert out == ""  # stdout stays pristine (TOON / --json safety)
    assert "0.37.0" in err
    assert "uv tool upgrade caffeinated-whale-cli" in err


def test_hidden_when_up_to_date(monkeypatch, capsys):
    monkeypatch.delenv(update_notice._ENV_DISABLE, raising=False)
    monkeypatch.setattr(update_notice, "_stderr_is_tty", lambda: True)
    monkeypatch.setattr("caffeinated_whale_cli.core.version.passive_notice", lambda: None)

    update_notice.notify_if_outdated()

    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_suppressed_when_stderr_not_a_tty(monkeypatch, capsys):
    monkeypatch.delenv(update_notice._ENV_DISABLE, raising=False)
    monkeypatch.setattr(update_notice, "_stderr_is_tty", lambda: False)
    fake, calls = _fake_notice(_OUTDATED)
    monkeypatch.setattr("caffeinated_whale_cli.core.version.passive_notice", fake)

    update_notice.notify_if_outdated()

    out, err = capsys.readouterr()
    assert out == "" and err == ""
    assert calls == []  # gated before the core is even consulted


def test_suppressed_by_env_var(monkeypatch, capsys):
    monkeypatch.setenv(update_notice._ENV_DISABLE, "1")
    monkeypatch.setattr(update_notice, "_stderr_is_tty", lambda: True)
    fake, calls = _fake_notice(_OUTDATED)
    monkeypatch.setattr("caffeinated_whale_cli.core.version.passive_notice", fake)

    update_notice.notify_if_outdated()

    out, err = capsys.readouterr()
    assert out == "" and err == ""
    assert calls == []  # env var short-circuits before any work


def test_fail_open_when_core_raises(monkeypatch, capsys):
    monkeypatch.delenv(update_notice._ENV_DISABLE, raising=False)
    monkeypatch.setattr(update_notice, "_stderr_is_tty", lambda: True)

    def boom():
        raise RuntimeError("network exploded")

    monkeypatch.setattr("caffeinated_whale_cli.core.version.passive_notice", boom)

    update_notice.notify_if_outdated()  # must not raise

    out, err = capsys.readouterr()
    assert out == "" and err == ""
