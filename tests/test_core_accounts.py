"""The web UI's administrator account: hashes, the forced change, sessions, lockout."""

from __future__ import annotations

import json

import pytest

from caffeinated_whale_cli.core import accounts
from caffeinated_whale_cli.core.errors import CwcliError

T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    yield


class TestDefaultAccount:
    def test_first_start_creates_admin_admin_with_the_change_forced(self):
        assert accounts.ensure_default(now=T0) is True
        assert accounts.ensure_default(now=T0) is False  # idempotent
        acct = accounts.verify("admin", "admin")
        assert acct is not None and acct.must_change is True

    def test_the_file_holds_no_password_and_is_private(self):
        accounts.ensure_default(now=T0)
        raw = accounts.store_path().read_text()
        assert 'admin"' in raw and '"admin"' not in json.dumps(json.loads(raw)["accounts"]["admin"])
        assert accounts.store_path().stat().st_mode & 0o777 == 0o600

    def test_a_wrong_password_and_an_unknown_user_both_answer_none(self):
        accounts.ensure_default(now=T0)
        assert accounts.verify("admin", "nope") is None
        assert accounts.verify("nobody", "admin") is None


class TestPasswordChange:
    def test_the_forced_change_needs_no_current_password_and_clears_the_flag(self):
        accounts.ensure_default(now=T0)
        accounts.change_password("admin", "correct horse", now=T0)
        acct = accounts.verify("admin", "correct horse")
        assert acct is not None and acct.must_change is False
        assert accounts.verify("admin", "admin") is None

    def test_a_later_change_needs_the_current_password(self):
        accounts.ensure_default(now=T0)
        accounts.change_password("admin", "correct horse", now=T0)
        with pytest.raises(CwcliError) as e:
            accounts.change_password("admin", "battery staple", current="wrong", now=T0)
        assert e.value.code == "password.current_wrong"
        accounts.change_password("admin", "battery staple", current="correct horse", now=T0)
        assert accounts.verify("admin", "battery staple") is not None

    @pytest.mark.parametrize("bad", ["short", "admin", "ADMIN", "a" * 7])
    def test_weak_passwords_are_refused(self, bad):
        accounts.ensure_default(now=T0)
        with pytest.raises(CwcliError) as e:
            accounts.change_password("admin", bad, now=T0)
        assert e.value.code == "password.weak"

    def test_reset_returns_to_admin_admin_forces_the_change_and_closes_sessions(self):
        accounts.ensure_default(now=T0)
        accounts.change_password("admin", "correct horse", now=T0)
        sid = accounts.open_session("admin", remember=False, now=T0)
        accounts.reset_password(now=T0)
        acct = accounts.verify("admin", "admin")
        assert acct is not None and acct.must_change is True
        assert accounts.session_account(sid, now=T0) is None

    def test_reset_on_a_shared_install_needs_root(self, monkeypatch):
        accounts.ensure_default(now=T0)
        monkeypatch.setattr(accounts.shared_home, "shared_mode", lambda: True)
        monkeypatch.setattr(accounts.os, "geteuid", lambda: 1000, raising=False)
        with pytest.raises(CwcliError) as e:
            accounts.reset_password(now=T0)
        assert e.value.code == "accounts.needs_root"


class TestSessions:
    def test_a_session_signs_in_until_idle_or_absolute_expiry(self):
        accounts.ensure_default(now=T0)
        sid = accounts.open_session("admin", remember=False, now=T0)
        assert accounts.session_account(sid, now=T0 + 7 * 3600).username == "admin"
        # kept alive by use: 7h, then another 7h
        assert accounts.session_account(sid, now=T0 + 14 * 3600) is not None
        # idle 8h+ -> gone
        assert accounts.session_account(sid, now=T0 + 14 * 3600 + accounts.IDLE_S + 1) is None

    def test_the_absolute_limit_ends_a_busy_session_too(self):
        accounts.ensure_default(now=T0)
        sid = accounts.open_session("admin", remember=False, now=T0)
        t = T0
        while t < T0 + accounts.ABSOLUTE_S - 3600:
            t += 3600
            assert accounts.session_account(sid, now=t) is not None
        assert accounts.session_account(sid, now=T0 + accounts.ABSOLUTE_S + 1) is None

    def test_keep_me_signed_in_lasts_thirty_days(self):
        accounts.ensure_default(now=T0)
        sid = accounts.open_session("admin", remember=True, now=T0)
        assert accounts.session_account(sid, now=T0 + 20 * 86400) is not None
        assert accounts.session_account(sid, now=T0 + 31 * 86400) is None

    def test_the_file_stores_only_a_hash_of_the_session_id(self):
        accounts.ensure_default(now=T0)
        sid = accounts.open_session("admin", remember=False, now=T0)
        assert sid not in accounts.store_path().read_text()
        assert accounts.session_account("forged", now=T0) is None

    def test_sign_out_here_and_everywhere_else(self):
        accounts.ensure_default(now=T0)
        a = accounts.open_session("admin", remember=False, agent="A", now=T0)
        b = accounts.open_session("admin", remember=False, agent="B", now=T0 + 1)
        c = accounts.open_session("admin", remember=False, agent="C", now=T0 + 2)
        assert [s.agent for s in accounts.list_sessions("admin", current=a, now=T0 + 2)] == [
            "C",
            "B",
            "A",
        ]
        assert accounts.close_other_sessions("admin", keep=a) == 2
        assert accounts.session_account(b, now=T0) is None
        assert accounts.session_account(c, now=T0) is None
        accounts.close_session(a)
        assert accounts.session_account(a, now=T0) is None


class TestLockout:
    def test_five_failures_lock_the_key_for_a_minute(self):
        lo = accounts.Lockout()
        for i in range(4):
            lo.failed("admin", "10.0.0.1", now=T0 + i)
        assert lo.retry_after("admin", now=T0 + 4) == 0
        lo.failed("admin", "10.0.0.1", now=T0 + 4)
        assert 0 < lo.retry_after("admin", now=T0 + 4) <= accounts.LOCKOUT_S
        assert lo.retry_after("10.0.0.1", now=T0 + 4) > 0  # the address is locked too
        assert lo.retry_after("admin", now=T0 + 4 + accounts.LOCKOUT_S) == 0

    def test_old_failures_fall_out_of_the_window_and_success_clears(self):
        lo = accounts.Lockout()
        for i in range(4):
            lo.failed("admin", now=T0 + i)
        lo.failed("admin", now=T0 + accounts.LOCKOUT_WINDOW_S + 10)  # the four are stale
        assert lo.retry_after("admin", now=T0 + accounts.LOCKOUT_WINDOW_S + 10) == 0
        for i in range(5):
            lo.failed("other", now=T0 + i)
        lo.succeeded("other")
        assert lo.retry_after("other", now=T0 + 5) == 0
