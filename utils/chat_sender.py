"""Отправка сообщений в чат Team Fortress 2 через эмуляцию клавиатуры."""

import ctypes
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from ctypes import wintypes

from pynput.keyboard import Controller, Key, KeyCode


MAX_MESSAGE_BYTES = 127
TRUNCATION_SUFFIX = "..."
TF2_WINDOW_CLASS = "Valve001"
CHAT_KEYS = {
    "all": KeyCode.from_vk(0x59),  # VK_Y — физическая клавиша Y
    "team": KeyCode.from_vk(0x55),  # VK_U — физическая клавиша U
}

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_find_window = _user32.FindWindowW
_find_window.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_find_window.restype = wintypes.HWND
_get_foreground_window = _user32.GetForegroundWindow
_get_foreground_window.argtypes = []
_get_foreground_window.restype = wintypes.HWND


class ChatSender:
    """Асинхронно вводит сообщение в выбранный чат TF2."""

    def __init__(self) -> None:
        self._keyboard = Controller()
        self._state_lock = threading.Lock()
        self._send_slots = threading.BoundedSemaphore(8)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tf2-chat-sender",
        )
        self._closed = False

    def send(self, text: str, chat_mode: str = "all") -> bool:
        """Поставить отправку в ограниченную последовательную очередь."""
        if chat_mode not in CHAT_KEYS:
            print(
                f"Unsupported chat mode: {chat_mode!r}",
                file=sys.stderr,
            )
            return False

        if not self._send_slots.acquire(blocking=False):
            print("TF2 chat send queue is full", file=sys.stderr)
            return False

        prepared_text = self._truncate_text(text)
        with self._state_lock:
            if self._closed:
                self._send_slots.release()
                return False

            try:
                future = self._executor.submit(
                    self._do_send,
                    prepared_text,
                    chat_mode,
                )
            except RuntimeError as error:
                self._send_slots.release()
                print(f"Failed to schedule chat sender: {error}", file=sys.stderr)
                return False

        future.add_done_callback(self._release_send_slot)

        return True

    def shutdown(self) -> None:
        """Запретить новые отправки и отменить ещё не начатые задачи."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True

        self._executor.shutdown(wait=False, cancel_futures=True)

    def _do_send(self, text: str, chat_mode: str) -> None:
        """Открыть чат, ввести текст и подтвердить отправку."""
        try:
            if not self._is_tf2_foreground():
                return

            chat_key = CHAT_KEYS[chat_mode]
            self._keyboard.press(chat_key)
            self._keyboard.release(chat_key)

            time.sleep(0.08)
            if not self._is_tf2_foreground():
                return

            self._keyboard.type(text)

            self._keyboard.press(Key.enter)
            self._keyboard.release(Key.enter)
            time.sleep(0.05)
        except Exception as error:
            print(f"Failed to send TF2 chat message: {error}", file=sys.stderr)

    def _release_send_slot(self, _future: Future) -> None:
        self._send_slots.release()

    @staticmethod
    def _is_tf2_foreground() -> bool:
        tf2_window = _find_window(TF2_WINDOW_CLASS, None)
        return bool(tf2_window) and _get_foreground_window() == tf2_window

    @staticmethod
    def _truncate_text(text: str) -> str:
        encoded_text = text.encode("utf-8")
        if len(encoded_text) <= MAX_MESSAGE_BYTES:
            return text

        encoded_suffix = TRUNCATION_SUFFIX.encode("utf-8")
        content_bytes = encoded_text[: MAX_MESSAGE_BYTES - len(encoded_suffix)]
        content = content_bytes.decode("utf-8", errors="ignore")
        return content + TRUNCATION_SUFFIX


if __name__ == "__main__":
    sender = ChatSender()
    test_text = input("Message to send: ")
    test_mode = input("Chat mode [all/team] (default: all): ").strip() or "all"

    print("Focus the TF2 window. Sending in 3 seconds...")
    time.sleep(3.0)

    if sender.send(test_text, test_mode):
        # ChatSender uses a daemon thread, so keep this demo alive until it finishes.
        time.sleep(1.0)
        print("Send task started.")
    else:
        print("Send task was not started.", file=sys.stderr)
