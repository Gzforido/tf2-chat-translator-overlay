"""Interactive local credential setup; never embeds keys in source files."""

import argparse
import getpass
import secrets
import sys

from utils.config_loader import CONFIG_PATH, DEFAULT_CONFIG, load_config, save_config


KEY_FIELDS = (
    ("deepl_api_key", "DeepL API key (required for chat translation)"),
    ("steam_api_key", "Steam Web API key (optional, inventory scanner)"),
    ("backpacktf_api_key", "backpack.tf legacy API key (optional, prices)"),
    ("backpacktf_access_token", "backpack.tf access token (optional, C/S values)"),
)


def _has_required_values(config: dict[str, object]) -> bool:
    return bool(
        str(config.get("deepl_api_key", "")).strip()
        and str(config.get("rcon_password", "")).strip()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-required", action="store_true")
    args = parser.parse_args()

    if args.check_required:
        if not CONFIG_PATH.is_file():
            return 1
        return 0 if _has_required_values(load_config()) else 1

    if CONFIG_PATH.is_file():
        # Refuse to overwrite a broken config; the user may need to repair it.
        import json

        try:
            with CONFIG_PATH.open("r", encoding="utf-8-sig") as source:
                if not isinstance(json.load(source), dict):
                    raise ValueError("config.json must contain a JSON object")
        except (OSError, ValueError) as error:
            print(f"Cannot read config.json: {error}", file=sys.stderr)
            return 1

    config = load_config() if CONFIG_PATH.is_file() else DEFAULT_CONFIG.copy()
    print("Paste your own keys. Input is hidden; Enter keeps the existing value.")
    print("Optional keys can be left empty.")

    try:
        for field, label in KEY_FIELDS:
            current = str(config.get(field, "")).strip()
            state = "set" if current else "empty"
            entered = getpass.getpass(f"{label} [{state}]: ").strip()
            if entered:
                config[field] = entered
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled; no changes were saved.", file=sys.stderr)
        return 1

    if not str(config.get("rcon_password", "")).strip():
        config["rcon_password"] = secrets.token_urlsafe(24)
        print("Generated a unique local RCON password.")

    try:
        save_config(config)
    except OSError as error:
        print(f"Cannot save config.json: {error}", file=sys.stderr)
        return 1

    print("Saved local config.json. This file is excluded from Git.")
    if not str(config.get("deepl_api_key", "")).strip():
        print("DeepL key is required before the application can start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
