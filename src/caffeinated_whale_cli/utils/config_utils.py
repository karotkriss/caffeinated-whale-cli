import os
from pathlib import Path

import toml

APP_NAME = ".cwcli"


def cwcli_home() -> Path:
    """Return the base directory for cwcli's on-disk state.

    All of cwcli's footprint - config, the projects registry, the cache
    database, and the auto-inspect run directory - lives under this directory.
    The ``CWCLI_HOME`` environment variable, when set to a non-empty value,
    relocates that footprint wholesale in place of the default ``~/.cwcli``,
    WITHOUT repointing the process ``HOME`` (so git, ssh, and other
    HOME-derived tooling are unaffected).

    ``config_utils`` and ``db_utils`` both resolve their paths through this one
    helper, so they can never disagree on where cwcli's state lives.
    """
    override = os.environ.get("CWCLI_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / APP_NAME


CONFIG_DIR: Path = cwcli_home() / "config"
CONFIG_FILE: Path = CONFIG_DIR / "config.toml"
PROJECTS_DIR: Path = cwcli_home() / "projects"

DEFAULT_CONFIG_CONTENT = """
# Caffeinated Whale CLI Configuration
# You can add custom absolute paths here for the `inspect` command to search for benches.

[search_paths]
# A list of custom directories where your Frappe bench instances are located.
# The `inspect` command will search these paths in addition to the defaults.
# Example:
# custom_bench_paths = [
#   "/Users/your_user/projects/frappe_benches",
#   "/opt/shared_benches",
# ]
custom_bench_paths = []

[auto_inspect]
# Automatic project inspection settings
# When enabled, cwcli will automatically inspect all running projects periodically
# to keep cached data fresh for tab completion and other features.

# Enable or disable automatic inspection (true/false)
enabled = false

# Inspection interval in seconds (default: 3600 = 1 hour)
# Minimum: 60 seconds (1 minute)
# Recommended: 3600 seconds (1 hour)
interval = 3600

# Start auto-inspect on system boot/login (true/false)
# When true, the auto-inspect background process will start automatically
# Platform-specific: Uses LaunchAgent (macOS), systemd (Linux), or Task Scheduler (Windows)
startup_enabled = false

[ui]
# User interface settings

# Show contextual tips during long-running operations (true/false)
# Tips help you discover features and best practices while waiting
show_tips = true
"""


def _ensure_config_exists():
    """Ensures the config directory and a default config file exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.is_file():
        with open(CONFIG_FILE, "w") as f:
            f.write(DEFAULT_CONFIG_CONTENT)


def load_config() -> dict:
    """Loads the configuration from the TOML file."""
    _ensure_config_exists()
    with open(CONFIG_FILE) as f:
        try:
            config_data = toml.load(f)
            if "search_paths" not in config_data:
                config_data["search_paths"] = {}
            if "custom_bench_paths" not in config_data["search_paths"]:
                config_data["search_paths"]["custom_bench_paths"] = []
            if "auto_inspect" not in config_data:
                config_data["auto_inspect"] = {
                    "enabled": False,
                    "interval": 3600,
                    "startup_enabled": False,
                }
            elif "startup_enabled" not in config_data["auto_inspect"]:
                config_data["auto_inspect"]["startup_enabled"] = False
            if "ui" not in config_data:
                config_data["ui"] = {"show_tips": True}
            elif "show_tips" not in config_data["ui"]:
                config_data["ui"]["show_tips"] = True
            return config_data
        except toml.TomlDecodeError:
            return {
                "search_paths": {"custom_bench_paths": []},
                "auto_inspect": {"enabled": False, "interval": 3600, "startup_enabled": False},
                "ui": {"show_tips": True},
            }


def save_config(config_data: dict):
    """Saves the given configuration data to the TOML file."""
    _ensure_config_exists()
    with open(CONFIG_FILE, "w") as f:
        toml.dump(config_data, f)


def add_custom_path(path: str) -> bool:
    """Adds a new path to the custom search paths."""
    config = load_config()
    if path not in config["search_paths"]["custom_bench_paths"]:
        config["search_paths"]["custom_bench_paths"].append(path)
        save_config(config)
        return True
    return False


def remove_custom_path(path: str) -> bool:
    """Removes a path from the custom search paths."""
    config = load_config()
    if path in config["search_paths"]["custom_bench_paths"]:
        config["search_paths"]["custom_bench_paths"].remove(path)
        save_config(config)
        return True
    return False


def get_auto_inspect_config() -> dict:
    """Get auto-inspect configuration."""
    config = load_config()
    auto_inspect: dict = config.get(
        "auto_inspect", {"enabled": False, "interval": 3600, "startup_enabled": False}
    )
    return auto_inspect


def read_auto_inspect_config() -> dict:
    """Read auto-inspect configuration without creating config state."""
    default = {"enabled": False, "interval": 3600, "startup_enabled": False}
    if not CONFIG_FILE.is_file():
        return default
    try:
        with open(CONFIG_FILE) as f:
            config = toml.load(f)
    except (OSError, toml.TomlDecodeError):
        return default
    auto_inspect = config.get("auto_inspect", default)
    return auto_inspect if isinstance(auto_inspect, dict) else default


def set_auto_inspect_enabled(enabled: bool):
    """Enable or disable auto-inspect."""
    config = load_config()
    if "auto_inspect" not in config:
        config["auto_inspect"] = {"enabled": enabled, "interval": 3600}
    else:
        config["auto_inspect"]["enabled"] = enabled
    save_config(config)


def set_auto_inspect_interval(interval: int):
    """Set auto-inspect interval in seconds (minimum 60)."""
    if interval < 60:
        raise ValueError("Interval must be at least 60 seconds")
    config = load_config()
    if "auto_inspect" not in config:
        config["auto_inspect"] = {"enabled": False, "interval": interval, "startup_enabled": False}
    else:
        config["auto_inspect"]["interval"] = interval
    save_config(config)


def set_auto_inspect_startup(enabled: bool):
    """Enable or disable auto-inspect on system startup."""
    config = load_config()
    if "auto_inspect" not in config:
        config["auto_inspect"] = {"enabled": False, "interval": 3600, "startup_enabled": enabled}
    else:
        config["auto_inspect"]["startup_enabled"] = enabled
    save_config(config)


def get_show_tips() -> bool:
    """
    Get whether tips should be shown during long-running operations.

    Returns:
        True if tips should be shown, False otherwise (default: True)
    """
    config = load_config()
    show_tips: bool = config.get("ui", {}).get("show_tips", True)
    return show_tips


def set_show_tips(enabled: bool):
    """
    Enable or disable tips during long-running operations.

    Args:
        enabled: True to show tips, False to hide them
    """
    config = load_config()
    if "ui" not in config:
        config["ui"] = {"show_tips": enabled}
    else:
        config["ui"]["show_tips"] = enabled
    save_config(config)
