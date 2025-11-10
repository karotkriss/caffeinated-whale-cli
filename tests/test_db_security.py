"""Tests for database security (permissions and sensitive data handling)."""

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

    def test_models_have_encryption_todo(self):
        """Test that models document need for encryption."""
        common_doc = db_utils.CommonSiteConfig.__doc__
        site_doc = db_utils.SiteConfig.__doc__

        assert "TODO" in common_doc, "CommonSiteConfig missing encryption TODO"
        assert "encryption" in common_doc.lower(), "CommonSiteConfig doesn't mention encryption"

        assert "TODO" in site_doc, "SiteConfig missing encryption TODO"
        assert "encryption" in site_doc.lower(), "SiteConfig doesn't mention encryption"


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
