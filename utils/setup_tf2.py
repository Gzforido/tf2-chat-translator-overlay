"""Автоматическая настройка TF2 для работы с RCON и логированием чата."""

import ctypes
import json
import re
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_find_window = _user32.FindWindowW
_find_window.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_find_window.restype = wintypes.HWND
_set_foreground = _user32.SetForegroundWindow
_set_foreground.argtypes = [wintypes.HWND]
_set_foreground.restype = wintypes.BOOL
_get_foreground = _user32.GetForegroundWindow
_get_foreground.argtypes = []
_get_foreground.restype = wintypes.HWND

_MARKER = "# TF2ChatTranslatorOverlay"
_TF2_APP_ID = "440"


# ──────────────────────────── config ────────────────────────────

def _load_config() -> dict:
    config_path = Path("config.json")
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# ──────────────────────────── Steam path ────────────────────────────

def _find_steam_path() -> Path | None:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        path_str, _ = winreg.QueryValueEx(key, "SteamPath")
        winreg.CloseKey(key)
        p = Path(path_str)
        return p if p.exists() else None
    except Exception:
        return None


def _find_localconfig_files(steam_path: Path) -> list[Path]:
    userdata = steam_path / "userdata"
    if not userdata.exists():
        return []
    return [
        entry / "config" / "localconfig.vdf"
        for entry in userdata.iterdir()
        if entry.is_dir() and (entry / "config" / "localconfig.vdf").exists()
    ]


# ──────────────────────────── localconfig.vdf ────────────────────────────

def _patch_localconfig(vdf_path: Path, rcon_password: str) -> bool:
    """
    Добавляет +rcon_password и +net_start в LaunchOptions TF2 (app 440).
    Безопасно: ищет раздел "440" и модифицирует только его LaunchOptions.
    """
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[setup] Не могу прочитать {vdf_path}: {e}", file=sys.stderr)
        return False

    rcon_cmd = f"+rcon_password {rcon_password}"
    net_cmd = "+net_start"

    # Если уже прописано — пропускаем
    if rcon_cmd in text:
        return True

    lines = text.splitlines(keepends=True)
    new_lines: list[str] = []
    in_440 = False
    depth = 0
    opened = False
    patched = False

    for line in lines:
        stripped = line.strip()

        if not in_440:
            # Ищем начало раздела "440"
            if re.fullmatch(r'"440"', stripped):
                in_440 = True
                depth = 0
                opened = False
        else:
            opens = stripped.count("{")
            closes = stripped.count("}")
            depth += opens - closes

            if opens > 0 and not opened:
                opened = True

            if opened and depth == 0:
                # Вышли из раздела "440"
                in_440 = False

            # LaunchOptions находится на глубине 1
            if opened and depth == 1 and not patched:
                lo_match = re.match(
                    r'(\s*"LaunchOptions"\s*")([^"]*?)(")', line
                )
                if lo_match:
                    existing = lo_match.group(2)
                    additions = []
                    if rcon_cmd not in existing:
                        additions.append(rcon_cmd)
                    if net_cmd not in existing:
                        additions.append(net_cmd)
                    if additions:
                        new_opts = (existing + " " + " ".join(additions)).strip()
                        line = (
                            lo_match.group(1)
                            + new_opts
                            + lo_match.group(3)
                            + "\n"
                        )
                        print(f"[setup] Steam launch options обновлены: {vdf_path}")
                        print("[setup] RCON settings updated (password hidden)")
                    patched = True

        new_lines.append(line)

    if not patched:
        print(
            "[setup] Раздел LaunchOptions для TF2 не найден — "
            "проверь настройки запуска вручную",
            file=sys.stderr,
        )
        return False

    try:
        vdf_path.write_text("".join(new_lines), encoding="utf-8")
        return True
    except OSError as e:
        print(f"[setup] Не могу записать {vdf_path}: {e}", file=sys.stderr)
        return False


# ──────────────────────────── autoexec.cfg ────────────────────────────

def _find_cfg_dir(config: dict) -> Path | None:
    log_path = Path(str(config.get("log_path", "")))
    cfg_dir = log_path.parent / "cfg"
    return cfg_dir if cfg_dir.is_dir() else None


def _patch_autoexec(cfg_dir: Path, rcon_password: str) -> None:
    autoexec_path = cfg_dir / "autoexec.cfg"

    existing = ""
    if autoexec_path.exists():
        try:
            existing = autoexec_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    if _MARKER in existing:
        return

    needed: list[str] = []
    if "rcon_password" not in existing:
        needed.append(f"rcon_password {rcon_password}")
    if "net_start" not in existing:
        needed.append("net_start")

    if not needed:
        return

    block = f"\n{_MARKER}\n" + "\n".join(needed) + "\n"
    try:
        with autoexec_path.open("a", encoding="utf-8") as f:
            f.write(block)
        print(f"[setup] autoexec.cfg обновлён: {autoexec_path}")
        print("[setup] RCON settings updated (password hidden)")
    except OSError as error:
        print(f"[setup] Не удалось записать autoexec.cfg: {error}", file=sys.stderr)


# ──────────────────────────── entry point ────────────────────────────

def run() -> None:
    config = _load_config()
    rcon_password = str(config.get("rcon_password", "")).strip()
    if not rcon_password:
        print("[setup] RCON password is empty; run setup_api.bat first", file=sys.stderr)
        return

    # 1. Прописать RCON-команды в Steam launch options
    steam_path = _find_steam_path()
    if steam_path:
        for vdf_path in _find_localconfig_files(steam_path):
            _patch_localconfig(vdf_path, rcon_password)
    else:
        print("[setup] Steam не найден — пропускаем патч launch options", file=sys.stderr)

    # 2. Прописать в autoexec.cfg (резервный вариант)
    cfg_dir = _find_cfg_dir(config)
    if cfg_dir:
        _patch_autoexec(cfg_dir, rcon_password)
    else:
        print(
            "[setup] Папка cfg TF2 не найдена — проверь log_path в config.json",
            file=sys.stderr,
        )


# ──────────────────────────── console injection ────────────────────────────

_get_window_thread_process_id = _user32.GetWindowThreadProcessId
_get_window_thread_process_id.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_get_window_thread_process_id.restype = wintypes.DWORD
_attach_thread_input = _user32.AttachThreadInput
_attach_thread_input.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_attach_thread_input.restype = wintypes.BOOL
_get_current_thread_id = ctypes.windll.kernel32.GetCurrentThreadId
_get_current_thread_id.argtypes = []
_get_current_thread_id.restype = wintypes.DWORD


def _force_foreground(hwnd: int) -> bool:
    """Принудительно переводит фокус на окно через AttachThreadInput."""
    tf2_tid = _get_window_thread_process_id(hwnd, None)
    our_tid = _get_current_thread_id()
    if tf2_tid != our_tid:
        _attach_thread_input(our_tid, tf2_tid, True)
    _set_foreground(hwnd)
    if tf2_tid != our_tid:
        _attach_thread_input(our_tid, tf2_tid, False)
    time.sleep(0.3)
    return _get_foreground() == hwnd


def inject_rcon_password(rcon_password: str) -> bool:
    """
    Открывает консоль TF2 и вводит rcon_password + net_start.
    Возвращает True если TF2 найден и команды отправлены.
    """
    tf2_hwnd = _find_window("Valve001", None)
    if not tf2_hwnd:
        return False

    try:
        from pynput.keyboard import Controller, Key, KeyCode
    except ImportError:
        return False

    kb = Controller()
    prev_hwnd = _get_foreground()

    try:
        if not _force_foreground(tf2_hwnd):
            print("[setup] Не удалось перевести фокус на TF2", file=sys.stderr)
            return False

        time.sleep(0.2)

        console_key = KeyCode.from_vk(0xC0)  # ~ (VK_OEM_3)
        kb.press(console_key)
        kb.release(console_key)
        time.sleep(0.4)

        kb.type(f"rcon_password {rcon_password}")
        time.sleep(0.15)
        kb.press(Key.enter)
        kb.release(Key.enter)
        time.sleep(0.3)

        kb.type("net_start")
        time.sleep(0.15)
        kb.press(Key.enter)
        kb.release(Key.enter)
        time.sleep(0.3)

        kb.press(console_key)
        kb.release(console_key)

        print("[setup] RCON пароль установлен в TF2 через консоль")
        return True
    except Exception as error:
        print(f"[setup] Ошибка инжекта в консоль TF2: {error}", file=sys.stderr)
        return False
    finally:
        if prev_hwnd and prev_hwnd != tf2_hwnd:
            try:
                _force_foreground(prev_hwnd)
            except Exception:
                pass


if __name__ == "__main__":
    run()
