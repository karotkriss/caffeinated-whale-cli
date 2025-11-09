import toml
from pathlib import Path
from typing import Dict

APP_NAME = "caffeinated-whale-cli"
CONFIG_DIR: Path = Path.home() / APP_NAME / "config"
CONFIG_FILE: Path = CONFIG_DIR / "config.toml"

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
"""


def _ensure_config_exists():
    """Ensures the config directory and a default config file exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.is_file():
        with open(CONFIG_FILE, "w") as f:
            f.write(DEFAULT_CONFIG_CONTENT)


def load_config() -> Dict:
    """Loads the configuration from the TOML file."""
    _ensure_config_exists()
    with open(CONFIG_FILE, "r") as f:
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
            return config_data
        except toml.TomlDecodeError:
            return {
                "search_paths": {"custom_bench_paths": []},
                "auto_inspect": {"enabled": False, "interval": 3600, "startup_enabled": False},
            }


def save_config(config_data: Dict):
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


def get_auto_inspect_config() -> Dict:
    """Get auto-inspect configuration."""
    config = load_config()
    return config.get(
        "auto_inspect", {"enabled": False, "interval": 3600, "startup_enabled": False}
    )


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
