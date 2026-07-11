"""Tests for ``bench new-site`` MariaDB flag selection per Frappe branch.

The ``--mariadb-user-host-login-scope`` flag only exists in bench/Frappe 15+,
so versions 13 and 14 must fall back to ``--no-mariadb-socket``. Passing the
newer flag to bench 14 makes ``bench new-site`` fail.
"""

import pytest

from caffeinated_whale_cli.commands.init import _select_mariadb_flag


class TestSelectMariadbFlag:
    """Test cases for ``_select_mariadb_flag``."""

    def test_version_13_uses_no_mariadb_socket(self):
        assert _select_mariadb_flag("version-13") == "--no-mariadb-socket"

    def test_version_14_uses_no_mariadb_socket(self):
        # Regression: bench 14 does not understand
        # --mariadb-user-host-login-scope, so it must use --no-mariadb-socket.
        assert _select_mariadb_flag("version-14") == "--no-mariadb-socket"

    @pytest.mark.parametrize("branch", ["version-15", "version-16", "develop"])
    def test_version_15_plus_uses_host_login_scope(self, branch):
        assert _select_mariadb_flag(branch) == "--mariadb-user-host-login-scope=%"

    @pytest.mark.parametrize(
        "ref,expected",
        [
            ("v14.80.0", "--no-mariadb-socket"),
            ("v13.60.0", "--no-mariadb-socket"),
            ("v15.40.0", "--mariadb-user-host-login-scope=%"),
            ("v16.26.3", "--mariadb-user-host-login-scope=%"),
        ],
    )
    def test_tag_refs_gate_on_major_version(self, ref, expected):
        # Regression: a semver tag (from --version X.Y.Z) must gate the same
        # way its branch equivalent does, not silently fall to the 15+ default.
        assert _select_mariadb_flag(ref) == expected
