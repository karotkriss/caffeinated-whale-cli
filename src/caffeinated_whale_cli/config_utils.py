import typer
import toml
from pathlib import Path
from typing import Dict, List

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
            return config_data
        except toml.TomlDecodeError:
            return {"search_paths": {"custom_bench_paths": []}}


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
