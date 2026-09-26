"""Загрузка и сохранение конфигурации приложения."""

import json
import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    APPLICATION_DIR = Path(sys.executable).resolve().parent
else:
    APPLICATION_DIR = Path(__file__).resolve().parent.parent

CONFIG_PATH = APPLICATION_DIR / "config.json"

DEFAULT_CONFIG: dict[str, object] = {
    "deepl_api_key": "",
    "log_path": (
        "C:/Program Files (x86)/Steam/steamapps/common/"
        "Team Fortress 2/tf/console.log"
    ),
    "target_lang": "RU",
    "reverse_target_lang": "EN-US",
    "overlay_x": 50,
    "overlay_y": 50,
    "overlay_width": 0,
    "overlay_height": 0,
    "max_messages": 8,
    "chat_mode": "all",
    "hotkey_toggle_drag": "ins",
    "hotkey_input_window": "home",
    "hotkey_toggle_chat_mode": "scroll_lock",
    "font_size": 14,
    "steam_api_key": "",
    "backpacktf_api_key": "",
    "backpacktf_access_token": "",
    "rcon_password": "",
    "rcon_host": "auto",
    "rcon_port": 27015,
    "inventory_min_price": 0,
    "inventory_team_filter": "all",
    "inventory_overlay_x": 600,
    "inventory_overlay_y": 50,
    "inventory_overlay_width": 0,
    "inventory_overlay_height": 0,
}


def load_config() -> dict[str, object]:
    """Загрузить конфигурацию, создав файл со значениями по умолчанию."""
    if not CONFIG_PATH.exists():
        config = DEFAULT_CONFIG.copy()
        save_config(config)
        return config

    try:
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as config_file:
            loaded = json.load(config_file)
    except (json.JSONDecodeError, OSError) as error:
        print(f"config.json error: {error}, using defaults", file=sys.stderr)
        return DEFAULT_CONFIG.copy()

    if not isinstance(loaded, dict):
        print(
            "config.json must be a JSON object, using defaults",
            file=sys.stderr,
        )
        return DEFAULT_CONFIG.copy()

    return {**DEFAULT_CONFIG, **loaded}


def save_config(config: dict[str, object]) -> None:
    """Атомарно сохранить конфигурацию рядом с main.py."""
    temporary_path = CONFIG_PATH.with_suffix(".json.tmp")

    with temporary_path.open("w", encoding="utf-8") as config_file:
        json.dump(config, config_file, ensure_ascii=False, indent=2)
        config_file.write("\n")

    os.replace(temporary_path, CONFIG_PATH)
