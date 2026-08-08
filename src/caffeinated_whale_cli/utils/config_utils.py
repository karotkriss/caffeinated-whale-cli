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

[cred_bridge]
# Persistent git credential bridge (opt-in). When enabled, a detached host
# daemon lets in-container git reach your host's authenticated gh/glab for
# private-repo fetches during interactive work (cwcli open, plain docker exec),
# not just during cwcli's own app operations. The raw token NEVER enters the
# container. Turn it on with `cwcli config cred-bridge enable`.
enabled = false

# Start the bridge daemon at system boot/login (true/false)
# Managed by `cwcli config cred-bridge enable --startup` / `--no-startup`.
# Platform-specific: Uses LaunchAgent (macOS), systemd (Linux), or Task Scheduler (Windows)
startup_enabled = false

# Hosts the persistent daemon will answer credential requests for. Each entry
# matches the request's `host=` field exactly (include the port if non-default,
# e.g. "git.corp.example:8443"). Add your self-hosted GitLab/GitHub host here.
# Applies immediately (no daemon restart needed); cwcli's own per-operation
# bridge (apps install/update, init) is not restricted by this list.
allowed_hosts = ["github.com", "gitlab.com"]
"""

DEFAULT_ALLOWED_HOSTS = ("github.com", "gitlab.com")


def _cred_bridge_defaults() -> dict:
    """A FRESH default [cred_bridge] section (fresh list, never a shared mutable)."""
    return {
        "enabled": False,
        "startup_enabled": False,
        "allowed_hosts": list(DEFAULT_ALLOWED_HOSTS),
    }


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
            if "cred_bridge" not in config_data:
                config_data["cred_bridge"] = _cred_bridge_defaults()
            else:
                for key, value in _cred_bridge_defaults().items():
                    config_data["cred_bridge"].setdefault(key, value)
            return config_data
        except toml.TomlDecodeError:
            return {
                "search_paths": {"custom_bench_paths": []},
                "auto_inspect": {"enabled": False, "interval": 3600, "startup_enabled": False},
                "ui": {"show_tips": True},
                "cred_bridge": _cred_bridge_defaults(),
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
    with open(CONFIG_FILE) as f:
        config = toml.load(f)
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


def get_cred_bridge_config() -> dict:
    """Get persistent credential-bridge configuration (creates config if absent)."""
    config = load_config()
    cred_bridge: dict = config.get("cred_bridge", _cred_bridge_defaults())
    return cred_bridge


def read_cred_bridge_config() -> dict:
    """Read credential-bridge config WITHOUT creating config state.

    Used on the hot path (the ensure step in ``open``/``core.start``), so it must
    never write a default config file into the user's home as a side effect - it
    just reports ``enabled: false`` when there is nothing to read.
    """
    default = _cred_bridge_defaults()
    if not CONFIG_FILE.is_file():
        return default
    try:
        with open(CONFIG_FILE) as f:
            config = toml.load(f)
    except (OSError, toml.TomlDecodeError):
        return default
    cred_bridge = config.get("cred_bridge", default)
    return cred_bridge if isinstance(cred_bridge, dict) else default


def set_cred_bridge_enabled(enabled: bool):
    """Enable or disable the persistent credential bridge."""
    config = load_config()
    if "cred_bridge" not in config:
        config["cred_bridge"] = _cred_bridge_defaults()
    config["cred_bridge"]["enabled"] = enabled
    save_config(config)


def set_cred_bridge_startup(enabled: bool):
    """Enable or disable starting the credential-bridge daemon at boot/login."""
    config = load_config()
    if "cred_bridge" not in config:
        config["cred_bridge"] = _cred_bridge_defaults()
    config["cred_bridge"]["startup_enabled"] = enabled
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
