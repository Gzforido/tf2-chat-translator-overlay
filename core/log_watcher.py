"""Фоновое чтение новых строк из console.log Team Fortress 2."""

import sys
import threading
from pathlib import Path
from typing import Callable, Optional

if __package__:
    from .log_parser import ChatMessage, parse_line
else:
    from log_parser import ChatMessage, parse_line


POLL_INTERVAL_SECONDS = 0.1
LOG_ENCODING = "utf-8"
MAX_PENDING_BYTES = 1_048_576


class LogWatcher:
    """Следит за дописываемым логом TF2 и передаёт сообщения в callback."""

    def __init__(
        self,
        log_path: str,
        on_message: Callable[[ChatMessage], None],
        on_line: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._log_path = Path(log_path)
        self._on_message = on_message
        self._on_line = on_line
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Запустить слежку в отдельном daemon-потоке."""
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._watch,
                name="tf2-log-watcher",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Остановить слежку и дождаться завершения фонового потока."""
        with self._state_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()

        # Callback выполняется в watcher-потоке и может сам вызвать stop().
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)

        with self._state_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def _watch(self) -> None:
        position: Optional[int] = None
        pending_data = b""
        file_identity: Optional[tuple[int, int]] = None

        while not self._stop_event.is_set():
            try:
                file_stat = self._log_path.stat()
                file_size = file_stat.st_size
                current_identity = (file_stat.st_dev, file_stat.st_ino)
            except FileNotFoundError:
                # Identity сохраняется, чтобы распознать замену после появления.
                position = None
                pending_data = b""
                self._stop_event.wait(POLL_INTERVAL_SECONDS)
                continue
            except OSError:
                self._stop_event.wait(POLL_INTERVAL_SECONDS)
                continue

            if (
                file_identity is not None
                and file_identity != current_identity
            ):
                # Новый файл читается с начала независимо от его размера.
                file_identity = current_identity
                position = 0
                pending_data = b""
            elif position is None:
                # Существующие строки при старте не считаются новыми.
                position = file_size
                file_identity = current_identity
            elif file_size < position:
                # TF2 мог обнулить лог при перезапуске.
                position = 0
                pending_data = b""

            if file_size > position:
                try:
                    with self._log_path.open("rb") as log_file:
                        log_file.seek(position)
                        new_data = log_file.read()
                        position = log_file.tell()
                except FileNotFoundError:
                    position = None
                    pending_data = b""
                    self._stop_event.wait(POLL_INTERVAL_SECONDS)
                    continue
                except OSError:
                    self._stop_event.wait(POLL_INTERVAL_SECONDS)
                    continue

                if new_data:
                    pending_data += new_data
                    if len(pending_data) > MAX_PENDING_BYTES:
                        pending_data = b""
                        print(
                            "Log watcher pending line exceeded 1048576 bytes; "
                            "buffer reset",
                            file=sys.stderr,
                        )
                        self._stop_event.wait(POLL_INTERVAL_SECONDS)
                        continue

                    lines = pending_data.split(b"\n")
                    pending_data = lines.pop()

                    for raw_line in lines:
                        if self._stop_event.is_set():
                            break

                        line = raw_line.rstrip(b"\r").decode(
                            LOG_ENCODING,
                            errors="replace",
                        )
                        if self._on_line is not None:
                            try:
                                self._on_line(line)
                            except Exception as error:
                                print(
                                    f"Log watcher line callback failed: {error}",
                                    file=sys.stderr,
                                )
                        message = parse_line(line)
                        if message is not None:
                            try:
                                self._on_message(message)
                            except Exception as error:
                                print(
                                    f"Log watcher callback failed: {error}",
                                    file=sys.stderr,
                                )

            self._stop_event.wait(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    import sys
    import time

    def print_message(message: ChatMessage) -> None:
        chat_scope = "TEAM" if message.is_team else "ALL"
        print(f"[{chat_scope}] {message.player_name}: {message.text}")

    watched_log_path = sys.argv[1] if len(sys.argv) > 1 else "console.log"
    watcher = LogWatcher(watched_log_path, print_message)
    watcher.start()

    print(f"Watching {Path(watched_log_path).resolve()}")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        watcher.stop()
        print("Watcher stopped.")
