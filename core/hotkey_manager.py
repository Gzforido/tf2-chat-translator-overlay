"""Управление глобальными горячими клавишами приложения через pynput."""

import sys
import threading
from typing import Callable, Optional

from pynput import keyboard


ACTION_CONFIG_KEYS = {
    "toggle_drag": "hotkey_toggle_drag",
    "input_window": "hotkey_input_window",
    "toggle_chat_mode": "hotkey_toggle_chat_mode",
}

DEFAULT_HOTKEYS = {
    "toggle_drag": "ins",
    "input_window": "home",
    "toggle_chat_mode": "scroll_lock",
}

SPECIAL_KEY_ALIASES = {
    "ins": "insert",
    "del": "delete",
    "esc": "esc",
    "return": "enter",
    "pgup": "page_up",
    "pgdn": "page_down",
    "home": "home",
    "scroll_lock": "scroll_lock",
}


class HotkeyManager:
    """Регистрирует и обновляет глобальные горячие клавиши."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._hotkeys = {
            action: config.get(config_key, DEFAULT_HOTKEYS[action])
            for action, config_key in ACTION_CONFIG_KEYS.items()
        }
        self._listener: Optional[keyboard.GlobalHotKeys] = None
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()

    def register(self, action: str, callback: Callable[[], None]) -> None:
        """Зарегистрировать callback, выполняемый в listener-потоке pynput.

        Для изменения PyQt6 UI callback должен отправлять pyqtSignal или
        использовать QMetaObject.invokeMethod, а не вызывать QWidget напрямую.
        """
        self._validate_action(action)

        with self._state_lock:
            self._callbacks[action] = callback
            should_restart = self._listener is not None

        if should_restart:
            self._restart_listener()

    def start(self) -> None:
        """Создать и запустить daemon-listener с корректными хоткеями."""
        with self._state_lock:
            if self._listener is not None and self._listener.is_alive():
                return

            hotkey_mapping = self._build_hotkey_mapping()
            if not hotkey_mapping:
                print("No valid hotkeys to start", file=sys.stderr)
                self._listener = None
                return

            self._stop_event.clear()
            try:
                listener = keyboard.GlobalHotKeys(hotkey_mapping)
                listener.daemon = True
                listener.start()
            except Exception as error:
                print(f"Failed to start hotkey listener: {error}", file=sys.stderr)
                self._listener = None
                return

            self._listener = listener

            def _monitor_listener() -> None:
                listener.join()
                with self._state_lock:
                    stopped_expectedly = (
                        self._stop_event.is_set()
                        or self._listener is not listener
                    )

                if not stopped_expectedly:
                    print(
                        "HotkeyManager: listener stopped unexpectedly",
                        file=sys.stderr,
                    )

            threading.Thread(
                target=_monitor_listener,
                name="hotkey-monitor",
                daemon=True,
            ).start()

    def is_running(self) -> bool:
        """Вернуть ``True``, если listener активен."""
        with self._state_lock:
            return (
                self._listener is not None
                and self._listener.is_alive()
            )

    def stop(self) -> bool:
        """Остановить listener и вернуть ``True`` после завершения потока."""
        with self._state_lock:
            self._stop_event.set()
            listener = self._listener

        if listener is None:
            return True

        try:
            listener.stop()
            if listener is not threading.current_thread():
                listener.join(timeout=1.0)
        except Exception as error:
            print(f"Failed to stop hotkey listener: {error}", file=sys.stderr)

        stopped = not listener.is_alive()
        if stopped:
            with self._state_lock:
                if self._listener is listener:
                    self._listener = None
        return stopped

    def update_hotkey(self, action: str, new_hotkey: str) -> None:
        """Обновить комбинацию и перезапустить активный listener."""
        self._validate_action(action)

        try:
            normalized_hotkey = self._normalize_hotkey(new_hotkey)
            keyboard.HotKey.parse(normalized_hotkey)
        except (TypeError, ValueError) as error:
            self._log_parse_error(action, new_hotkey, error)
            return

        with self._state_lock:
            self._hotkeys[action] = new_hotkey
            self._config[ACTION_CONFIG_KEYS[action]] = new_hotkey
            should_restart = self._listener is not None

        if should_restart:
            self._restart_listener()

    def _restart_listener(self) -> None:
        if self.stop():
            self.start()

    def _build_hotkey_mapping(self) -> dict[str, Callable[[], None]]:
        hotkey_mapping: dict[str, Callable[[], None]] = {}

        for action in ACTION_CONFIG_KEYS:
            callback = self._callbacks.get(action)
            if callback is None:
                continue

            raw_hotkey = self._hotkeys[action]
            try:
                normalized_hotkey = self._normalize_hotkey(raw_hotkey)
                keyboard.HotKey.parse(normalized_hotkey)
            except (TypeError, ValueError) as error:
                self._log_parse_error(action, raw_hotkey, error)
                continue

            if normalized_hotkey in hotkey_mapping:
                print(
                    f"Duplicate hotkey {normalized_hotkey!r} for action "
                    f"{action!r}; action skipped",
                    file=sys.stderr,
                )
                continue

            hotkey_mapping[normalized_hotkey] = self._make_callback(action)

        return hotkey_mapping

    def _make_callback(self, action: str) -> Callable[[], None]:
        def invoke_callback() -> None:
            with self._state_lock:
                callback = self._callbacks.get(action)

            if callback is None:
                return

            try:
                callback()
            except Exception as error:
                print(
                    f"Hotkey callback {action!r} failed: {error}",
                    file=sys.stderr,
                )

        return invoke_callback

    @staticmethod
    def _normalize_hotkey(hotkey: str) -> str:
        if not isinstance(hotkey, str):
            raise TypeError("hotkey must be a string")

        stripped_hotkey = hotkey.strip().lower()
        if not stripped_hotkey:
            raise ValueError("hotkey must not be empty")

        normalized_parts: list[str] = []
        for raw_part in stripped_hotkey.split("+"):
            part = raw_part.strip()
            if not part:
                raise ValueError("hotkey contains an empty key")

            if part.startswith("<") and part.endswith(">"):
                key_name = part[1:-1].strip()
                if not key_name:
                    raise ValueError("hotkey contains an empty special key")
                key_name = SPECIAL_KEY_ALIASES.get(key_name, key_name)
                normalized_parts.append(f"<{key_name}>")
            elif len(part) == 1:
                normalized_parts.append(part)
            else:
                key_name = SPECIAL_KEY_ALIASES.get(part, part)
                normalized_parts.append(f"<{key_name}>")

        return "+".join(normalized_parts)

    @staticmethod
    def _validate_action(action: str) -> None:
        if action not in ACTION_CONFIG_KEYS:
            supported_actions = ", ".join(ACTION_CONFIG_KEYS)
            raise ValueError(
                f"Unsupported action {action!r}; expected one of: "
                f"{supported_actions}"
            )

    @staticmethod
    def _log_parse_error(action: str, hotkey: object, error: Exception) -> None:
        print(
            f"Invalid hotkey for action {action!r}: {hotkey!r} ({error})",
            file=sys.stderr,
        )
