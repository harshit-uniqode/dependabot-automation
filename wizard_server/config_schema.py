"""Load, validate, and default config/wizard-config.json."""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config" / "wizard-config.json"

DEFAULT_CONFIG = {
    "version": 1,
    "server": {
        "port": 8787,
        "host": "127.0.0.1",
        "emulator_endpoint": "http://localhost:4566",
        "job_ttl_seconds": 3600,
        "max_queue_depth": 3,
    },
    "repos": [
        {
            "name": "beaconstac_angular_portal",
            "path": "/Users/harshitky/Desktop/Work/beaconstac_angular_portal",
            "github": "mobstac-private/beaconstac_angular_portal",
            "type": "angular",
            "package_dir": "bac-app",
            "default_base_branch": "develop",
            "test_commands": {
                "build": "npx ng build --configuration production",
                "unit_test": "npx ng test --watch=false --browsers=ChromeHeadless",
            },
        },
        {
            "name": "beaconstac_lambda_functions",
            "path": "/Users/harshitky/Desktop/Work/beaconstac_lambda_functions",
            "github": "mobstac-private/beaconstac_lambda_functions",
            "type": "lambda_monorepo",
            "default_base_branch": "develop",
        },
    ],
}


def load_config():
    """Load config from disk, or create default if missing. Returns dict."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        # Merge missing top-level keys from defaults
        for key in DEFAULT_CONFIG:
            if key not in cfg:
                cfg[key] = DEFAULT_CONFIG[key]
        # Merge missing server keys
        for key in DEFAULT_CONFIG["server"]:
            if key not in cfg.get("server", {}):
                cfg.setdefault("server", {})[key] = DEFAULT_CONFIG["server"][key]
        return cfg

    # First run — create default config
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    """Write config to disk."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def validate_repo(repo):
    """Validate a repo entry. Returns (is_valid, error_message)."""
    required = ["name", "path", "github"]
    for field in required:
        if not repo.get(field):
            return False, f"Missing required field: {field}"

    path = Path(repo["path"])
    if not path.exists():
        return False, f"Path does not exist: {repo['path']}"
    if not path.is_dir():
        return False, f"Path is not a directory: {repo['path']}"

    return True, None


def add_repo(cfg, repo_data):
    """Add or update a repo in config. Returns updated config."""
    # Check if repo already exists by name
    for i, existing in enumerate(cfg["repos"]):
        if existing["name"] == repo_data["name"]:
            cfg["repos"][i] = {**existing, **repo_data}
            save_config(cfg)
            return cfg

    cfg["repos"].append(repo_data)
    save_config(cfg)
    return cfg
