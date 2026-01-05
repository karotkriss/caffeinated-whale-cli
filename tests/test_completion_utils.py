"""
Tests for tab completion utilities.

Tests cover completion functions for projects, apps, and sites with scenarios
including caching, TTL expiration, error handling, and context-awareness.
"""

import time
from unittest.mock import Mock, patch

import pytest
import typer
from docker.errors import DockerException

from caffeinated_whale_cli.utils import completion_utils


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset completion cache before each test."""
    completion_utils._cache.clear()
    completion_utils._docker_client = None
    yield
    completion_utils._cache.clear()
    completion_utils._docker_client = None


@pytest.fixture
def mock_docker_client():
    """Mock Docker client for testing."""
    client = Mock()
    client.ping = Mock()
    return client


@pytest.fixture
def mock_containers():
    """Mock Docker containers with project labels."""
    container1 = Mock()
    container1.labels = {
        "com.docker.compose.project": "frappe-one",
        "com.docker.compose.service": "frappe",
    }

    container2 = Mock()
    container2.labels = {
        "com.docker.compose.project": "frappe-two",
        "com.docker.compose.service": "frappe",
    }

    container3 = Mock()
    container3.labels = {
        "com.docker.compose.project": "frappe-one",  # Duplicate project
        "com.docker.compose.service": "nginx",
    }

    return [container1, container2, container3]


@pytest.fixture
def mock_cached_project_data():
    """Mock cached project data for app/site completions."""
    return {
        "project_name": "frappe-one",
        "bench_instances": [
            {
                "path": "/workspace/frappe-bench",
                "available_apps": ["frappe", "erpnext", "hrms"],
                "sites": [
                    {
                        "name": "site1.localhost",
                        "installed_apps": ["frappe", "erpnext"],
                    },
                    {
                        "name": "site2.localhost",
                        "installed_apps": ["frappe", "hrms"],
                    },
                ],
            },
            {
                "path": "/workspace/bench2",
                "available_apps": ["frappe", "custom_app"],
                "sites": [
                    {
                        "name": "site3.localhost",
                        "installed_apps": ["frappe"],
                    },
                ],
            },
        ],
    }


class TestCompleteProjectNames:
    """Tests for complete_project_names function."""

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_returns_unique_sorted_project_names(
        self, mock_from_env, mock_docker_client, mock_containers
    ):
        """Should return unique, sorted project names from Docker containers."""
        mock_from_env.return_value = mock_docker_client
        mock_docker_client.containers.list.return_value = mock_containers

        result = completion_utils.complete_project_names()

        assert result == ["frappe-one", "frappe-two"]
        mock_docker_client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": "com.docker.compose.service=frappe"},
        )

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_caches_results(self, mock_from_env, mock_docker_client, mock_containers):
        """Should cache results and not query Docker on second call."""
        mock_from_env.return_value = mock_docker_client
        mock_docker_client.containers.list.return_value = mock_containers

        # First call
        result1 = completion_utils.complete_project_names()
        # Second call
        result2 = completion_utils.complete_project_names()

        assert result1 == result2
        # Docker should only be queried once
        assert mock_docker_client.containers.list.call_count == 1

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_cache_expires_after_ttl(self, mock_from_env, mock_docker_client, mock_containers):
        """Should re-query Docker after cache TTL expires."""
        mock_from_env.return_value = mock_docker_client
        mock_docker_client.containers.list.return_value = mock_containers

        # First call
        completion_utils.complete_project_names()

        # Wait for cache to expire
        time.sleep(2.1)

        # Second call after TTL
        completion_utils.complete_project_names()

        # Docker should be queried twice
        assert mock_docker_client.containers.list.call_count == 2

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_returns_empty_list_on_docker_error(self, mock_from_env):
        """Should return empty list if Docker is unavailable."""
        mock_from_env.side_effect = DockerException("Docker not running")

        result = completion_utils.complete_project_names()

        assert result == []

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_handles_containers_without_project_label(self, mock_from_env, mock_docker_client):
        """Should handle containers missing project label."""
        container = Mock()
        container.labels = {}  # No project label

        mock_from_env.return_value = mock_docker_client
        mock_docker_client.containers.list.return_value = [container]

        result = completion_utils.complete_project_names()

        assert result == []

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_handles_empty_container_list(self, mock_from_env, mock_docker_client):
        """Should handle case with no containers."""
        mock_from_env.return_value = mock_docker_client
        mock_docker_client.containers.list.return_value = []

        result = completion_utils.complete_project_names()

        assert result == []

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_reuses_docker_client(self, mock_from_env, mock_docker_client, mock_containers):
        """Should reuse Docker client instance across calls."""
        mock_from_env.return_value = mock_docker_client
        mock_docker_client.containers.list.return_value = mock_containers

        # Clear cache to force Docker query
        completion_utils._cache.clear()
        completion_utils.complete_project_names()

        # Second call with cache cleared
        completion_utils._cache.clear()
        completion_utils.complete_project_names()

        # Docker client should only be created once
        assert mock_from_env.call_count == 1


class TestCompleteAppNames:
    """Tests for complete_app_names function."""

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_returns_sorted_app_names_from_cache(self, mock_get_cached, mock_cached_project_data):
        """Should return unique sorted app names from all benches."""
        mock_get_cached.return_value = mock_cached_project_data

        ctx = Mock(spec=typer.Context)
        ctx.params = {"project_name": "frappe-one"}

        result = completion_utils.complete_app_names(ctx)

        # Apps from both benches: frappe, erpnext, hrms, custom_app
        assert result == ["custom_app", "erpnext", "frappe", "hrms"]

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_returns_empty_list_when_no_project_in_context(self, mock_get_cached):
        """Should return empty list if project_name not in context."""
        ctx = Mock(spec=typer.Context)
        ctx.params = {}  # No project_name

        result = completion_utils.complete_app_names(ctx)

        assert result == []
        mock_get_cached.assert_not_called()

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_returns_empty_list_when_no_cache_data(self, mock_get_cached):
        """Should return empty list if no cached data available."""
        mock_get_cached.return_value = None

        ctx = Mock(spec=typer.Context)
        ctx.params = {"project_name": "frappe-one"}

        result = completion_utils.complete_app_names(ctx)

        assert result == []

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_caches_results_per_project(self, mock_get_cached, mock_cached_project_data):
        """Should cache results per project."""
        mock_get_cached.return_value = mock_cached_project_data

        ctx = Mock(spec=typer.Context)
        ctx.params = {"project_name": "frappe-one"}

        # First call
        result1 = completion_utils.complete_app_names(ctx)
        # Second call
        result2 = completion_utils.complete_app_names(ctx)

        assert result1 == result2
        # DB should only be queried once
        assert mock_get_cached.call_count == 1

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_handles_missing_bench_instances(self, mock_get_cached):
        """Should handle cached data without bench_instances key."""
        mock_get_cached.return_value = {"project_name": "frappe-one"}

        ctx = Mock(spec=typer.Context)
        ctx.params = {"project_name": "frappe-one"}

        result = completion_utils.complete_app_names(ctx)

        assert result == []

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_handles_exception_gracefully(self, mock_get_cached):
        """Should return empty list on any exception."""
        mock_get_cached.side_effect = Exception("Database error")

        ctx = Mock(spec=typer.Context)
        ctx.params = {"project_name": "frappe-one"}

        result = completion_utils.complete_app_names(ctx)

        assert result == []


class TestCompleteSiteNames:
    """Tests for complete_site_names function."""

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_returns_sorted_site_names_from_cache(self, mock_get_cached, mock_cached_project_data):
        """Should return unique sorted site names from all benches."""
        mock_get_cached.return_value = mock_cached_project_data

        ctx = Mock(spec=typer.Context)
        ctx.params = {"project_name": "frappe-one"}

        result = completion_utils.complete_site_names(ctx)

        # Sites from both benches
        assert result == ["site1.localhost", "site2.localhost", "site3.localhost"]

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_returns_empty_list_when_no_project_in_context(self, mock_get_cached):
        """Should return empty list if project_name not in context."""
        ctx = Mock(spec=typer.Context)
        ctx.params = {}

        result = completion_utils.complete_site_names(ctx)

        assert result == []
        mock_get_cached.assert_not_called()

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_caches_results_per_project(self, mock_get_cached, mock_cached_project_data):
        """Should cache results per project."""
        mock_get_cached.return_value = mock_cached_project_data

        ctx = Mock(spec=typer.Context)
        ctx.params = {"project_name": "frappe-one"}

        # First call
        result1 = completion_utils.complete_site_names(ctx)
        # Second call
        result2 = completion_utils.complete_site_names(ctx)

        assert result1 == result2
        assert mock_get_cached.call_count == 1

    @patch("caffeinated_whale_cli.utils.completion_utils.db_utils.get_cached_project_data")
    def test_handles_sites_without_name(self, mock_get_cached):
        """Should skip sites missing name field."""
        data = {
            "project_name": "frappe-one",
            "bench_instances": [
                {
                    "path": "/workspace/frappe-bench",
                    "available_apps": ["frappe"],
                    "sites": [
                        {"name": "valid.localhost"},
                        {},  # Missing name
                    ],
                }
            ],
        }
        mock_get_cached.return_value = data

        ctx = Mock(spec=typer.Context)
        ctx.params = {"project_name": "frappe-one"}

        result = completion_utils.complete_site_names(ctx)

        assert result == ["valid.localhost"]


class TestCacheHelpers:
    """Tests for internal cache helper functions."""

    def test_get_cached_returns_none_when_not_cached(self):
        """Should return None if key not in cache."""
        result = completion_utils._get_cached("nonexistent")
        assert result is None

    def test_get_cached_returns_value_within_ttl(self):
        """Should return cached value within TTL."""
        completion_utils._set_cached("test_key", ["value1", "value2"])

        result = completion_utils._get_cached("test_key", ttl=1.0)

        assert result == ["value1", "value2"]

    def test_get_cached_returns_none_after_ttl(self):
        """Should return None after TTL expires."""
        completion_utils._set_cached("test_key", ["value1", "value2"])

        time.sleep(0.5)
        result = completion_utils._get_cached("test_key", ttl=0.1)

        assert result is None

    def test_set_cached_stores_value_with_timestamp(self):
        """Should store value with current timestamp."""
        completion_utils._set_cached("test_key", ["value"])

        assert "test_key" in completion_utils._cache
        timestamp, value = completion_utils._cache["test_key"]
        assert value == ["value"]
        assert isinstance(timestamp, float)


class TestGetDockerClient:
    """Tests for _get_docker_client helper function."""

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_creates_client_on_first_call(self, mock_from_env):
        """Should create Docker client on first call."""
        mock_client = Mock()
        mock_from_env.return_value = mock_client

        result = completion_utils._get_docker_client()

        assert result == mock_client
        mock_from_env.assert_called_once()

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_reuses_client_on_subsequent_calls(self, mock_from_env):
        """Should reuse cached client on subsequent calls."""
        mock_client = Mock()
        mock_from_env.return_value = mock_client

        # First call
        completion_utils._get_docker_client()
        # Second call
        result = completion_utils._get_docker_client()

        assert result == mock_client
        # Should only create client once
        assert mock_from_env.call_count == 1

    @patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
    def test_returns_none_on_exception(self, mock_from_env):
        """Should return None if Docker client creation fails."""
        mock_from_env.side_effect = Exception("Docker unavailable")

        result = completion_utils._get_docker_client()

        assert result is None
