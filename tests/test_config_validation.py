"""Tests for configuration JSON validation."""

import pytest

from caffeinated_whale_cli.utils.db_utils import _validate_config_json


class TestConfigValidation:
    """Test cases for _validate_config_json function."""

    def test_valid_config_passes(self):
        """Test that valid config data passes validation."""
        valid_config = {
            "db_host": "mariadb",
            "redis_cache": "redis://redis-cache:6379",
            "webserver_port": 8000,
        }
        # Should not raise
        _validate_config_json(valid_config, "test_config")

    def test_empty_dict_passes(self):
        """Test that empty config is valid (represents config with no custom settings)."""
        # Should not raise - empty dict is a valid config
        _validate_config_json({}, "test_config")

    def test_non_dict_raises_typeerror(self):
        """Test that non-dictionary input raises TypeError."""
        with pytest.raises(TypeError, match="must be a dictionary"):
            _validate_config_json("not a dict", "test_config")

        with pytest.raises(TypeError, match="must be a dictionary"):
            _validate_config_json(["list", "of", "items"], "test_config")

        with pytest.raises(TypeError, match="must be a dictionary"):
            _validate_config_json(123, "test_config")

    def test_nested_dict_passes(self):
        """Test that nested dictionaries pass validation."""
        nested_config = {
            "database": {"host": "mariadb", "port": 3306},
            "redis": {"cache": "redis://cache:6379", "queue": "redis://queue:6379"},
        }
        # Should not raise
        _validate_config_json(nested_config, "test_config")

    def test_config_with_various_types_passes(self):
        """Test that config with various JSON-compatible types passes."""
        config = {
            "string": "value",
            "number": 123,
            "float": 45.67,
            "boolean": True,
            "null": None,
            "array": [1, 2, 3],
            "nested": {"key": "value"},
        }
        # Should not raise
        _validate_config_json(config, "test_config")

    def test_oversized_config_raises_valueerror(self):
        """Test that config larger than 1MB raises ValueError."""
        # Create a config that exceeds 1MB
        large_value = "x" * 500_000  # 500KB string
        oversized_config = {
            "field1": large_value,
            "field2": large_value,
            "field3": large_value,  # Total > 1MB
        }

        with pytest.raises(ValueError, match="too large.*Maximum size is 1MB"):
            _validate_config_json(oversized_config, "test_config")

    def test_config_type_included_in_error_message(self):
        """Test that config_type parameter is used in error messages."""
        with pytest.raises(TypeError, match="site_config must be a dictionary"):
            _validate_config_json("invalid", "site_config")

        with pytest.raises(TypeError, match="my_custom_config must be a dictionary"):
            _validate_config_json(["not", "a", "dict"], "my_custom_config")

    def test_realistic_common_site_config(self):
        """Test validation with realistic common_site_config data."""
        common_config = {
            "background_workers": 1,
            "db_host": "mariadb",
            "default_site": "development.localhost",
            "file_watcher_port": 6787,
            "frappe_user": "frappe",
            "gunicorn_workers": 25,
            "live_reload": True,
            "rebase_on_pull": False,
            "redis_cache": "redis://redis-cache:6379",
            "redis_queue": "redis://redis-queue:6379",
            "redis_socketio": "redis://redis-queue:6379",
            "restart_supervisor_on_update": False,
            "restart_systemd_on_update": False,
            "serve_default_site": True,
            "server_script_enabled": 1,
            "shallow_clone": True,
            "socketio_port": 9000,
            "use_redis_auth": False,
            "webserver_port": 8000,
        }
        # Should not raise
        _validate_config_json(common_config, "common_site_config")

    def test_realistic_site_config(self):
        """Test validation with realistic site_config data."""
        site_config = {
            "db_name": "_54cc49b9a1aab38b",
            "db_password": "a6kQtoKWi592Ro0t",
            "db_type": "mariadb",
            "developer_mode": 1,
        }
        # Should not raise
        _validate_config_json(site_config, "site_config")

    def test_config_with_special_characters(self):
        """Test that config with special characters in values passes."""
        config = {
            "password": "p@ssw0rd!#$%",
            "url": "redis://user:pass@host:6379/0",
            "unicode": "日本語テスト",
        }
        # Should not raise
        _validate_config_json(config, "test_config")

    def test_validation_preserves_data_integrity(self):
        """Test that validation doesn't modify the input data."""
        original_config = {
            "key1": "value1",
            "key2": 123,
            "key3": {"nested": "value"},
        }
        config_copy = original_config.copy()

        _validate_config_json(config_copy, "test_config")

        # Verify data wasn't modified
        assert config_copy == original_config
