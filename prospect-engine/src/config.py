"""Load configuration files (settings, scoring weights, ICP)."""

import json
from pathlib import Path
from typing import Any

import yaml

from src.models import ICP

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"


def resolve(path: str | Path) -> Path:
    """Turn a path from config (relative to the project folder) into an absolute path."""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Read a YAML file into a dict (empty dict if the file is empty)."""
    with resolve(path).open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(path: str | Path = CONFIG_DIR / "settings.yaml") -> dict[str, Any]:
    """Load config/settings.yaml."""
    return load_yaml(path)


def load_scoring(path: str | Path = CONFIG_DIR / "scoring.yaml") -> dict[str, Any]:
    """Load config/scoring.yaml."""
    return load_yaml(path)


def load_icp(path: str | Path = CONFIG_DIR / "icp_default.json") -> ICP:
    """Load and validate an ICP JSON file."""
    with resolve(path).open(encoding="utf-8") as f:
        return ICP.model_validate(json.load(f))
