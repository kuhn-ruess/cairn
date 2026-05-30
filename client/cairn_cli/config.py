"""Persisted CLI settings: server host, API token, default project.

Stored as JSON at ``$XDG_CONFIG_HOME/cairn/config.json`` (falling back to
``~/.config/cairn/config.json``). The token is the only secret in here, so
the file is written with ``0600`` permissions.
"""
import json
import os
from pathlib import Path

DEFAULT_HOST = "https://app.cairn-app.de"

_ENV_HOST = "CAIRN_HOST"
_ENV_TOKEN = "CAIRN_TOKEN"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "cairn"


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then chmod so the token never sits in a world-readable file.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_host(cfg: dict) -> str:
    """Effective host: env override > saved value > default."""
    host = os.environ.get(_ENV_HOST) or cfg.get("host") or DEFAULT_HOST
    return host.rstrip("/")


def get_token(cfg: dict) -> str | None:
    return os.environ.get(_ENV_TOKEN) or cfg.get("token")
