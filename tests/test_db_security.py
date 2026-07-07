"""Tests for database security (permissions and sensitive data handling)."""

import json
import os
import stat
from pathlib import Path

import pytest

from caffeinated_whale_cli.utils import db_utils


class TestCacheDirectoryPermissions:
    """Test cases for cache directory security."""

    def test_cache_directory_has_restrictive_permissions(self):
        """Test that cache directory is created with 0700 permissions."""
        # Skip on Windows as it doesn't support Unix permissions
        if os.name == "nt":
            pytest.skip("Unix permissions not supported on Windows")

        if not db_utils.CACHE_DIR.exists():
            pytest.skip("Cache directory doesn't exist yet")

        # Get directory permissions
        dir_stat = db_utils.CACHE_DIR.stat()
        dir_mode = stat.S_IMODE(dir_stat.st_mode)

        # Should be 0700 (rwx------)
        expected_mode = 0o700
        assert (
            dir_mode == expected_mode
        ), f"Cache directory has permissions {oct(dir_mode)}, expected {oct(expected_mode)}"

    def test_cache_directory_not_world_readable(self):
        """Test that cache directory is not readable by others."""
        # Skip on Windows
        if os.name == "nt":
            pytest.skip("Unix permissions not supported on Windows")

        if not db_utils.CACHE_DIR.exists():
            pytest.skip("Cache directory doesn't exist yet")

        dir_stat = db_utils.CACHE_DIR.stat()
        dir_mode = stat.S_IMODE(dir_stat.st_mode)

        # Check that group and others have no permissions
        assert not (dir_mode & stat.S_IRWXG), "Cache directory is readable by group"
        assert not (dir_mode & stat.S_IRWXO), "Cache directory is readable by others"


class TestDatabaseFilePermissions:
    """Test cases for database file security."""

    def test_database_file_has_restrictive_permissions(self):
        """Test that database file is created with 0600 permissions."""
        # Skip on Windows
        if os.name == "nt":
            pytest.skip("Unix permissions not supported on Windows")

        # Initialize database to ensure it exists
        db_utils.initialize_database()

        if not db_utils.DB_PATH.exists():
            pytest.skip("Database file doesn't exist yet")

        # Get file permissions
        file_stat = db_utils.DB_PATH.stat()
        file_mode = stat.S_IMODE(file_stat.st_mode)

        # Should be 0600 (rw-------)
        expected_mode = 0o600
        assert (
            file_mode == expected_mode
        ), f"Database file has permissions {oct(file_mode)}, expected {oct(expected_mode)}"

    def test_database_file_not_world_readable(self):
        """Test that database file is not readable by others."""
        # Skip on Windows
        if os.name == "nt":
            pytest.skip("Unix permissions not supported on Windows")

        db_utils.initialize_database()

        if not db_utils.DB_PATH.exists():
            pytest.skip("Database file doesn't exist yet")

        file_stat = db_utils.DB_PATH.stat()
        file_mode = stat.S_IMODE(file_stat.st_mode)

        # Check that group and others have no read permissions
        assert not (file_mode & stat.S_IRGRP), "Database file is readable by group"
        assert not (file_mode & stat.S_IROTH), "Database file is readable by others"
        assert not (file_mode & stat.S_IWGRP), "Database file is writable by group"
        assert not (file_mode & stat.S_IWOTH), "Database file is writable by others"

    def test_set_secure_db_permissions_function(self):
        """Test that _set_secure_db_permissions sets correct permissions."""
        # Skip on Windows
        if os.name == "nt":
            pytest.skip("Unix permissions not supported on Windows")

        db_utils.initialize_database()

        if not db_utils.DB_PATH.exists():
            pytest.skip("Database file doesn't exist yet")

        # Manually change permissions to something insecure
        db_utils.DB_PATH.chmod(0o644)

        # Call the function to secure it
        db_utils._set_secure_db_permissions()

        # Verify it's now secure
        file_stat = db_utils.DB_PATH.stat()
        file_mode = stat.S_IMODE(file_stat.st_mode)
        assert file_mode == 0o600, f"Permissions not secured: {oct(file_mode)}"


class TestSensitiveDataWarnings:
    """Test that security warnings are properly documented."""

    def test_common_site_config_has_security_warning(self):
        """Test that CommonSiteConfig model has security documentation."""
        doc = db_utils.CommonSiteConfig.__doc__
        assert doc is not None, "CommonSiteConfig missing docstring"
        assert "SECURITY WARNING" in doc, "CommonSiteConfig missing security warning in docstring"
        assert "sensitive data" in doc.lower(), "Security warning doesn't mention sensitive data"

    def test_site_config_has_security_warning(self):
        """Test that SiteConfig model has security documentation."""
        doc = db_utils.SiteConfig.__doc__
        assert doc is not None, "SiteConfig missing docstring"
        assert "SECURITY WARNING" in doc, "SiteConfig missing security warning in docstring"
        assert (
            "database credentials" in doc.lower()
        ), "Security warning doesn't mention database credentials"

    def test_models_document_redaction(self):
        """Models document the redaction contract, and the encryption TODO is gone.

        The old TODO promised field-level encryption; issue #42 paid that debt by
        redacting secrets at write time instead. Inverting the old TODO assertion
        locks in that the debt is not silently reintroduced.
        """
        common_doc = db_utils.CommonSiteConfig.__doc__
        site_doc = db_utils.SiteConfig.__doc__

        assert "redact" in common_doc.lower(), "CommonSiteConfig doesn't document redaction"
        assert "redact" in site_doc.lower(), "SiteConfig doesn't document redaction"

        assert "TODO" not in common_doc, "CommonSiteConfig still carries the encryption TODO"
        assert "TODO" not in site_doc, "SiteConfig still carries the encryption TODO"


class TestSecurityBestPractices:
    """Test that we follow security best practices."""

    def test_no_hardcoded_credentials_in_code(self):
        """Test that no credentials are hardcoded in db_utils.py."""
        import inspect

        source = inspect.getsource(db_utils)

        # Check for common credential patterns (passwords, keys, etc.)
        dangerous_patterns = [
            "password=",
            "api_key=",
            "secret=",
            "token=",
            "credential=",
        ]

        for pattern in dangerous_patterns:
            # Allow these in comments and docstrings, but not in actual code
            lines = source.split("\n")
            for line in lines:
                # Skip comments and docstrings
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                # Check for pattern in actual code
                if pattern in line.lower() and "=" in line and not line.strip().startswith("#"):
                    # Make sure it's not just a variable name or in a string
                    if not (
                        f'"{pattern}"' in line
                        or f"'{pattern}'" in line
                        or "# " in line.split("=")[0]
                    ):
                        pytest.fail(f"Possible hardcoded credential found: {line.strip()[:100]}")

    def test_cache_dir_location_is_user_specific(self):
        """Test that cache directory is in user's home directory."""
        cache_dir_str = str(db_utils.CACHE_DIR)
        home_str = str(Path.home())

        assert cache_dir_str.startswith(
            home_str
        ), "Cache directory should be in user's home directory"


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point db_utils at a throwaway sqlite file, restoring the real one after.

    Mirrors the fixture in test_bench_label_db_and_command.py so these tests never
    touch the real ~/.cwcli cache.
    """
    orig_path = db_utils.DB_PATH
    dbfile = tmp_path / "cache.db"
    monkeypatch.setattr(db_utils, "DB_PATH", dbfile)
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(dbfile))
    db_utils.initialize_database()
    yield dbfile
    if not db_utils.db.is_closed():
        db_utils.db.close()
    # Restore the real DB binding so later tests in the session are unaffected.
    db_utils.db.init(str(orig_path))


class TestCacheRedaction:
    """The cache must strip secrets at write time (issue #42)."""

    def test_cache_strips_secrets_from_common_site_config(self, temp_db):
        db_utils.cache_project_data(
            "proj",
            [
                {
                    "path": "/workspace/frappe-bench",
                    "available_apps": [],
                    "sites": [],
                    "common_site_config": {
                        "default_site": "a.local",
                        "root_password": "super-secret",
                        "redis_cache": "redis://localhost:6379/0",
                        "encryption_key": "deadbeef",
                    },
                }
            ],
        )

        bench = db_utils.Bench.get(db_utils.Bench.path == "/workspace/frappe-bench")
        raw = db_utils.CommonSiteConfig.get(db_utils.CommonSiteConfig.bench == bench).config_json

        assert json.loads(raw) == {"default_site": "a.local"}
        assert "root_password" not in raw
        assert "redis" not in raw
        assert "encryption_key" not in raw

    def test_cache_strips_secrets_from_site_config(self, temp_db):
        db_utils.cache_project_data(
            "proj",
            [
                {
                    "path": "/workspace/frappe-bench",
                    "available_apps": [],
                    "sites": [
                        {
                            "name": "a.local",
                            "installed_apps": [],
                            "site_config": {
                                "db_name": "_abc123",
                                "db_password": "super-secret",
                                "encryption_key": "deadbeef",
                                "admin_password": "hunter2",
                            },
                        }
                    ],
                }
            ],
        )

        site = db_utils.Site.get(db_utils.Site.name == "a.local")
        raw = db_utils.SiteConfig.get(db_utils.SiteConfig.site == site).config_json

        assert json.loads(raw) == {"db_name": "_abc123"}
        assert "db_password" not in raw
        assert "encryption_key" not in raw
        assert "admin_password" not in raw

    def test_get_default_site_still_works_after_redaction(self, temp_db):
        db_utils.cache_project_data(
            "proj",
            [
                {
                    "path": "/workspace/frappe-bench",
                    "available_apps": [],
                    "sites": [],
                    "common_site_config": {"default_site": "a.local", "db_password": "x"},
                }
            ],
        )

        assert db_utils.get_default_site("proj") == "a.local"

    def test_scrub_cleans_old_secret_bearing_rows_on_init(self, temp_db):
        # Simulate a pre-redaction cache: write a config row verbatim, bypassing
        # cache_project_data's redaction, then prove initialize_database() scrubs it.
        db_utils.cache_project_data(
            "proj",
            [{"path": "/workspace/frappe-bench", "available_apps": [], "sites": []}],
        )
        bench = db_utils.Bench.get(db_utils.Bench.path == "/workspace/frappe-bench")
        secret_json = json.dumps(
            {"default_site": "a.local", "root_password": "leak", "redis_cache": "redis://x"}
        )
        db_utils.CommonSiteConfig.create(bench=bench, config_json=secret_json)

        db_utils.initialize_database()

        raw = db_utils.CommonSiteConfig.get(db_utils.CommonSiteConfig.bench == bench).config_json
        assert json.loads(raw) == {"default_site": "a.local"}
        assert "root_password" not in raw
        assert "redis" not in raw

    def test_scrub_is_idempotent_noop_on_clean_rows(self, temp_db):
        # A row already written through cache_project_data is clean; the scrub on a
        # second initialize_database() must not change it (no spurious rewrite).
        db_utils.cache_project_data(
            "proj",
            [
                {
                    "path": "/workspace/frappe-bench",
                    "available_apps": [],
                    "sites": [],
                    "common_site_config": {"default_site": "a.local", "db_password": "x"},
                }
            ],
        )
        bench = db_utils.Bench.get(db_utils.Bench.path == "/workspace/frappe-bench")
        before = db_utils.CommonSiteConfig.get(db_utils.CommonSiteConfig.bench == bench).config_json

        db_utils.initialize_database()

        after = db_utils.CommonSiteConfig.get(db_utils.CommonSiteConfig.bench == bench).config_json
        assert before == after == json.dumps({"default_site": "a.local"})
