"""Where Weitblick Remote keeps its files (XDG base directories).

- ~/.config/weitblick-remote/connections.json, settings.json
- ~/.local/share/weitblick-remote/FreeRDP/   FreeRDP's own state (license store)
- ~/.local/state/weitblick-remote/agent.log  audit log of AI agent tool calls
- $XDG_RUNTIME_DIR/weitblick-remote/mcp.sock the MCP socket (only while agent control is on)

The XDG_* variables are honored, so tests can point everything at a temporary directory.
"""

import os
from pathlib import Path

DIR_NAME = "weitblick-remote"
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def _xdg(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    base = Path(value) if value and os.path.isabs(value) else Path.home() / fallback
    return base / DIR_NAME


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / DIR_NAME
    return Path(f"/tmp/{DIR_NAME}-{os.getuid()}")


def connections_file() -> Path:
    return config_dir() / "connections.json"


def settings_file() -> Path:
    return config_dir() / "settings.json"


def freerdp_state_dir() -> Path:
    return data_dir() / "FreeRDP"


def agent_log_file() -> Path:
    return state_dir() / "agent.log"


def mcp_socket_path() -> Path:
    """WEITBLICK_MCP_SOCKET overrides the path (tests, several instances)."""
    override = os.environ.get("WEITBLICK_MCP_SOCKET")
    return Path(override) if override else runtime_dir() / "mcp.sock"


def clipboard_cache_dir() -> Path:
    """Files copied in Windows land here before they are pasted locally."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".cache"
    return root / DIR_NAME / "clipboard"


def bridge_library() -> Path:
    """libweitblick-bridge.so: $WEITBLICK_BRIDGE, the source tree's build/, or next to the installed package."""
    override = os.environ.get("WEITBLICK_BRIDGE")
    if override:
        return Path(override)
    candidates = [
        SOURCE_ROOT / "build" / "libweitblick-bridge.so",
        Path(__file__).resolve().parent / "lib" / "libweitblick-bridge.so",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def locale_dir() -> Path | None:
    for candidate in (SOURCE_ROOT / "build" / "locale", Path(__file__).resolve().parent / "locale"):
        if candidate.is_dir():
            return candidate
    return None


def icon_dir() -> Path:
    """The hicolor icons (`make` assembles them in build/icons) in the source tree or the installed package."""
    for candidate in (SOURCE_ROOT / "build" / "icons", Path(__file__).resolve().parent / "data" / "icons"):
        if candidate.is_dir():
            return candidate
    return SOURCE_ROOT / "data" / "icons"


def data_file(name: str) -> Path:
    """A file from data/ (icons, desktop file) in the source tree or the installed package."""
    for candidate in (SOURCE_ROOT / "data" / name, Path(__file__).resolve().parent / "data" / name):
        if candidate.exists():
            return candidate
    return SOURCE_ROOT / "data" / name
