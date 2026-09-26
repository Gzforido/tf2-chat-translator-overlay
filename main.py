"""Точка входа TF2 Chat Translator Overlay."""

import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Optional

from PyQt6.QtCore import QMetaObject, QObject, QPoint, QTimer, Qt, pyqtSlot
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from core.hotkey_manager import HotkeyManager
from core.inventory_scanner import InventoryScanner, PlayerInventory
from core.log_parser import ChatMessage
from core.log_watcher import LogWatcher
from core.price_fetcher import PriceFetcher
from core.rcon_client import RconClient, STEAMID64_BASE
from core.translator import TRANSLATION_ERROR, Translator
from ui.inventory_overlay import InventoryOverlay
from ui.overlay import OverlayWindow
from utils.chat_sender import ChatSender
from utils.config_loader import load_config, save_config
from utils.tf2_window import tf2_overlay_should_be_visible


DEFAULT_OVERLAY_POSITION = 50
DEFAULT_INVENTORY_OVERLAY_X = 600
DEFAULT_INVENTORY_OVERLAY_Y = 50
DEFAULT_RCON_PORT = 27015
INVENTORY_REFRESH_INTERVAL_MS = 5_000
OVERLAY_VISIBILITY_INTERVAL_MS = 300
DEFAULT_TARGET_LANGUAGE = "RU"
VALID_CHAT_MODES = {"all", "team"}


class ApplicationController(QObject):
    """Связывает фоновые компоненты с объектами главного Qt-потока."""

    def __init__(
        self,
        config: dict[str, object],
        translator: Translator,
        overlay: OverlayWindow,
        inventory_overlay: InventoryOverlay,
        input_window: QWidget,
        chat_sender: ChatSender,
        log_watcher: LogWatcher,
        hotkey_manager: HotkeyManager,
    ) -> None:
        super().__init__()
        self._config = config
        self._translator = translator
        self._overlay = overlay
        self._inventory_overlay = inventory_overlay
        self._input_window = input_window
        self._chat_sender = chat_sender
        self._log_watcher = log_watcher
        self._hotkey_manager = hotkey_manager

        rcon_port = max(
            1,
            min(
                65_535,
                _config_int(config, "rcon_port", DEFAULT_RCON_PORT),
            ),
        )
        self._rcon_client = RconClient(
            host=_config_str(config, "rcon_host", "auto"),
            port=rcon_port,
            password=_config_str(config, "rcon_password"),
        )
        self._price_fetcher = PriceFetcher(
            api_key=_config_str(config, "backpacktf_api_key"),
            access_token=_config_str(config, "backpacktf_access_token"),
        )
        self._inventory_scanner = InventoryScanner(
            steam_api_key=_config_str(config, "steam_api_key"),
            price_fetcher=self._price_fetcher,
        )

        self._drag_enabled = False
        self._shutting_down = threading.Event()
        self._translation_queue: Queue[tuple[int, str]] = Queue()
        self._message_sequence = 0
        self._pending_messages: dict[
            int,
            tuple[ChatMessage, Optional[str], float],
        ] = {}
        self._next_display_seq = 0

        self._inventory_ready = threading.Event()
        self._inventory_refresh_running = threading.Event()
        self._inventory_start_lock = threading.Lock()
        self._inventory_started = False
        self._active_player_ids_lock = threading.Lock()
        self._active_player_ids: set[str] = set()
        self._inventory_session_generation = 0
        self._inventory_result_queue: Queue[PlayerInventory] = Queue()
        self._inventory_removal_queue: Queue[str] = Queue()

        self._inventory_timer = QTimer(self)
        self._inventory_timer.setInterval(INVENTORY_REFRESH_INTERVAL_MS)
        self._inventory_timer.timeout.connect(self.refresh_inventories)
        self._inventory_overlay.refresh_requested.connect(
            self.request_inventory_refresh
        )

        self._translation_watchdog = QTimer(self)
        self._translation_watchdog.setInterval(1_000)
        self._translation_watchdog.timeout.connect(
            self._expire_translations
        )
        self._translation_watchdog.start()

        self._overlay_visibility_timer = QTimer(self)
        self._overlay_visibility_timer.setInterval(
            OVERLAY_VISIBILITY_INTERVAL_MS
        )
        self._overlay_visibility_timer.timeout.connect(
            self.sync_overlay_visibility
        )
        self._overlay_visibility_timer.start()

    @pyqtSlot()
    def sync_overlay_visibility(self) -> None:
        """Показывать оба оверлея только поверх активной TF2."""
        if self._shutting_down.is_set():
            return

        try:
            should_show = tf2_overlay_should_be_visible()
        except (OSError, AttributeError) as error:
            print(f"TF2 window detection failed: {error}", file=sys.stderr)
            self._overlay_visibility_timer.stop()
            return

        if should_show is None:
            return

        for overlay in (self._overlay, self._inventory_overlay):
            if should_show and not overlay.isVisible():
                overlay.show()
            elif not should_show and overlay.isVisible():
                overlay.hide()

    def on_log_message(self, message: ChatMessage) -> None:
        """Запустить перевод сообщения из watcher-потока."""
        if self._shutting_down.is_set():
            return

        target_language = self._config.get(
            "target_lang",
            DEFAULT_TARGET_LANGUAGE,
        )
        if not isinstance(target_language, str):
            target_language = DEFAULT_TARGET_LANGUAGE

        self._message_sequence += 1
        seq_id = self._message_sequence
        self._pending_messages[seq_id] = (
            message,
            None,
            time.monotonic() + 15.0,
        )
        self._translator.translate_async(
            message.text,
            target_language,
            lambda translation, sequence=seq_id: (
                self._queue_translation_ordered(sequence, translation)
            ),
        )

    def on_log_line(self, line: str) -> None:
        """Сразу убрать цены после выхода с игрового сервера."""
        if self._shutting_down.is_set():
            return
        if not line.startswith(
            (
                "Disconnect:",
                "Disconnecting from",
                "Disconnected from",
                "Connected to ",
            )
        ):
            return

        with self._active_player_ids_lock:
            self._inventory_session_generation += 1
            self._active_player_ids.clear()
        self._rcon_client.clear_player_cache()
        self._inventory_overlay.set_local_team("")
        self._inventory_overlay.clear_players()

    def request_ui_action(self, method_name: str) -> None:
        """Поставить Qt-slot в очередь главного потока."""
        if self._shutting_down.is_set():
            return

        try:
            QMetaObject.invokeMethod(
                self,
                method_name,
                Qt.ConnectionType.QueuedConnection,
            )
        except RuntimeError as error:
            print(
                f"Failed to queue UI action {method_name!r}: {error}",
                file=sys.stderr,
            )

    @pyqtSlot()
    def toggle_drag(self) -> None:
        """Переключить перетаскивание обоих оверлеев."""
        self._drag_enabled = not self._drag_enabled
        self._overlay.set_drag_enabled(self._drag_enabled)
        self._inventory_overlay.set_drag_enabled(self._drag_enabled)

        if not self._drag_enabled:
            self._save_overlay_position()

    @pyqtSlot()
    def toggle_input_window(self) -> None:
        """Показать или скрыть окно обратного перевода."""
        if self._input_window.isVisible():
            self._input_window.hide()
            return

        self._input_window.show()
        self._input_window.raise_()
        self._input_window.activateWindow()

    @pyqtSlot()
    def toggle_chat_mode(self) -> None:
        """Переключить all/team и сохранить выбранный режим."""
        current_mode = self._input_window.get_chat_mode()
        new_mode = "team" if current_mode == "all" else "all"

        self._input_window.set_chat_mode(new_mode)
        self._config["chat_mode"] = new_mode
        self._save_config_safely()

    @pyqtSlot()
    def flush_translations(self) -> None:
        """Добавить ожидающие переводы в оверлей из главного Qt-потока."""
        while True:
            try:
                seq_id, translation = self._translation_queue.get_nowait()
            except Empty:
                break

            if seq_id in self._pending_messages:
                message, current_translation, deadline = (
                    self._pending_messages[seq_id]
                )
                if current_translation is None:
                    self._pending_messages[seq_id] = (
                        message,
                        translation,
                        deadline,
                    )

        while self._next_display_seq + 1 in self._pending_messages:
            next_seq = self._next_display_seq + 1
            message, pending_translation, _ = self._pending_messages[next_seq]
            if pending_translation is None:
                break

            self._overlay.add_message(message, pending_translation)
            del self._pending_messages[next_seq]
            self._next_display_seq = next_seq

    @pyqtSlot()
    def _expire_translations(self) -> None:
        """Завершить просроченные переводы, не блокируя следующие сообщения."""
        now = time.monotonic()
        expired = False

        for seq_id, pending in list(self._pending_messages.items()):
            message, translation, deadline = pending
            if translation is None and now >= deadline:
                self._pending_messages[seq_id] = (
                    message,
                    TRANSLATION_ERROR,
                    deadline,
                )
                expired = True

        if expired:
            self.flush_translations()

    @pyqtSlot(str, str)
    def send_chat_message(self, text: str, chat_mode: str) -> None:
        """Передать готовый перевод фоновому отправителю чата."""
        if not self._chat_sender.send(text, chat_mode):
            print("TF2 chat send task was not started", file=sys.stderr)

    def start_inventory_services(self) -> None:
        """Загрузить цены и схему последовательно вне Qt-потока."""
        with self._inventory_start_lock:
            if self._inventory_started or self._shutting_down.is_set():
                return
            self._inventory_started = True

        threading.Thread(
            target=self._initialize_inventory_services,
            name="tf2-inventory-initializer",
            daemon=True,
        ).start()

    @pyqtSlot()
    def inventory_services_ready(self) -> None:
        """Запустить таймер и первый scan после загрузки справочников."""
        if self._shutting_down.is_set():
            return

        self._inventory_ready.set()
        self._inventory_timer.start()
        self.refresh_inventories()

    @pyqtSlot()
    def request_inventory_refresh(self) -> None:
        if self._shutting_down.is_set():
            return
        if not self._inventory_ready.is_set():
            self.start_inventory_services()
            return
        self.refresh_inventories()

    @pyqtSlot()
    def refresh_inventories(self) -> None:
        """Запустить чтение RCON и сканирование, не блокируя Qt."""
        if (
            self._shutting_down.is_set()
            or not self._inventory_ready.is_set()
            or self._inventory_refresh_running.is_set()
        ):
            return

        self._inventory_refresh_running.set()
        try:
            threading.Thread(
                target=self._refresh_inventories_worker,
                name="tf2-inventory-refresh",
                daemon=True,
            ).start()
        except RuntimeError as error:
            self._inventory_refresh_running.clear()
            print(
                f"Failed to start inventory refresh: {error}",
                file=sys.stderr,
            )

    def _on_inventory_result(self, inventory: PlayerInventory) -> None:
        """Поставить результат scanner-потока в очередь главного Qt-потока."""
        if self._shutting_down.is_set():
            return

        self._inventory_result_queue.put(inventory)
        self.request_ui_action("flush_inventory_results")

    @pyqtSlot()
    def flush_inventory_results(self) -> None:
        """Обновить таблицу инвентарей только для текущих игроков."""
        while True:
            try:
                inventory = self._inventory_result_queue.get_nowait()
            except Empty:
                return

            with self._active_player_ids_lock:
                is_current_player = inventory.steamid in self._active_player_ids
            if is_current_player:
                self._inventory_overlay.update_player(inventory)

    @pyqtSlot()
    def flush_inventory_removals(self) -> None:
        """Удалить вышедших игроков из оверлея в главном Qt-потоке."""
        while True:
            try:
                steamid = self._inventory_removal_queue.get_nowait()
            except Empty:
                return

            self._inventory_overlay.remove_player(steamid)

    @pyqtSlot()
    def shutdown(self) -> None:
        """Остановить фоновые компоненты и сохранить позиции окон."""
        if self._shutting_down.is_set():
            return

        self._shutting_down.set()
        self._inventory_timer.stop()
        self._translation_watchdog.stop()
        self._overlay_visibility_timer.stop()
        self._log_watcher.stop()
        self._hotkey_manager.stop()
        self._chat_sender.shutdown()
        self._save_overlay_position()

        threading.Thread(
            target=self._shutdown_workers,
            name="tf2-shutdown",
            daemon=False,
        ).start()

    def _shutdown_workers(self) -> None:
        """Освободить блокирующие ресурсы вне главного Qt-потока."""
        self._rcon_client.disconnect()
        self._inventory_scanner.shutdown()
        self._price_fetcher.shutdown()
        self._translator.shutdown()

    def _initialize_inventory_services(self) -> None:
        success = False
        try:
            print(
                "[inventory] loading backpack.tf prices",
                file=sys.stderr,
            )
            prices_loaded = self._price_fetcher.load_prices()
            print(
                "[inventory] backpack.tf prices loaded="
                f"{prices_loaded}, items="
                f"{len(self._price_fetcher._prices_db)}",
                file=sys.stderr,
            )
            if not prices_loaded:
                print(
                    "[inventory] price database is unavailable",
                    file=sys.stderr,
                )
                return
            if self._shutting_down.is_set():
                return

            print("[inventory] loading TF2 schema", file=sys.stderr)
            schema_loaded = self._inventory_scanner.load_schema()
            print(
                f"[inventory] TF2 schema loaded={schema_loaded}",
                file=sys.stderr,
            )
            if not schema_loaded:
                print(
                    "[inventory] TF2 schema is unavailable",
                    file=sys.stderr,
                )
                return
            if self._shutting_down.is_set():
                return

            success = True
            self.request_ui_action("inventory_services_ready")
        except Exception as error:
            print(
                f"[inventory] scanner initialization failed: {error}",
                file=sys.stderr,
            )
        finally:
            # Сбросить флаг при неудаче, чтобы кнопка ↻ могла запустить retry
            if not success:
                with self._inventory_start_lock:
                    self._inventory_started = False

    def _refresh_inventories_worker(self) -> None:
        try:
            with self._active_player_ids_lock:
                session_generation = self._inventory_session_generation
            players = self._get_rcon_players()
            if self._shutting_down.is_set():
                return
            if players is None:
                # Пропуск вывода status не доказывает, что игроки вышли.
                # Настоящий выход обрабатывает on_log_line().
                return
            local_steamid = _local_steamid64()
            local_team = next(
                (
                    str(player.get("team", ""))
                    for player in players
                    if player.get("steamid") == local_steamid
                ),
                "",
            )
            known_teams = sum(bool(player.get("team")) for player in players)
            print(
                f"[inventory] RCON returned {len(players)} players; "
                f"teams known={known_teams}; "
                f"local team={local_team or 'unknown'}",
                file=sys.stderr,
            )
            current_player_ids = {
                str(player.get("steamid", "")).strip()
                for player in players
                if str(player.get("steamid", "")).strip()
            }
            with self._active_player_ids_lock:
                if session_generation != self._inventory_session_generation:
                    return
                removed_player_ids = (
                    self._active_player_ids - current_player_ids
                )
                self._active_player_ids = current_player_ids

            self._inventory_overlay.set_player_teams(
                {
                    str(player.get("steamid", "")): str(player.get("team", ""))
                    for player in players
                    if str(player.get("steamid", "")).strip()
                }
            )
            self._inventory_overlay.set_local_team(local_team)

            for steamid in removed_player_ids:
                self._inventory_removal_queue.put(steamid)
            if removed_player_ids:
                self.request_ui_action("flush_inventory_removals")

            if not players:
                return

            futures = self._inventory_scanner.scan_players(
                players,
                on_result=self._on_inventory_result,
            )
            print(
                f"[inventory] inventory scan started {len(futures)} futures",
                file=sys.stderr,
            )
            if futures:
                from concurrent.futures import wait as futures_wait

                futures_wait(futures)
        except Exception as error:
            print(
                f"[inventory] refresh failed: {error}",
                file=sys.stderr,
            )
        finally:
            self._inventory_refresh_running.clear()

    def _get_rcon_players(self) -> Optional[list[dict]]:
        """Отличить недоступный RCON от успешно полученного пустого status."""
        players = self._rcon_client.get_players(
            Path(_config_str(self._config, "log_path"))
        )
        if players is None:
            print(
                "Inventory RCON/status unavailable; keeping current roster",
                file=sys.stderr,
            )
            return None
        return players

    def _queue_translation_ordered(
        self,
        seq_id: int,
        translation: str,
    ) -> None:
        if self._shutting_down.is_set():
            return

        self._translation_queue.put((seq_id, translation))
        self.request_ui_action("flush_translations")

    def _save_overlay_position(self) -> None:
        overlay_x, overlay_y = self._overlay.get_position()
        inventory_x, inventory_y = self._inventory_overlay.get_position()
        self._config["overlay_x"] = overlay_x
        self._config["overlay_y"] = overlay_y
        self._config["inventory_overlay_x"] = inventory_x
        self._config["inventory_overlay_y"] = inventory_y
        overlay_width, overlay_height = self._overlay.get_size()
        inventory_width, inventory_height = self._inventory_overlay.get_size()
        self._config["overlay_width"] = overlay_width
        self._config["overlay_height"] = overlay_height
        self._config["inventory_overlay_width"] = inventory_width
        self._config["inventory_overlay_height"] = inventory_height
        team_filter, min_price = self._inventory_overlay.get_filters()
        self._config["inventory_team_filter"] = team_filter
        self._config["inventory_min_price"] = min_price
        self._save_config_safely()

    def _save_config_safely(self) -> None:
        try:
            save_config(self._config)
        except OSError as error:
            print(f"Failed to save config: {error}", file=sys.stderr)


def _config_int(config: dict[str, object], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_float(
    config: dict[str, object],
    key: str,
    default: float = 0.0,
) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _config_str(
    config: dict[str, object],
    key: str,
    default: str = "",
) -> str:
    value = config.get(key, default)
    return value if isinstance(value, str) else default


def _local_steamid64() -> str:
    """Получить SteamID64 активного локального аккаунта без сетевых запросов."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Valve\Steam\ActiveProcess",
        ) as key:
            account_id, _ = winreg.QueryValueEx(key, "ActiveUser")
        account_id = int(account_id)
    except (OSError, TypeError, ValueError):
        return ""
    return str(STEAMID64_BASE + account_id) if account_id > 0 else ""


def main() -> int:
    config = load_config()
    app = QApplication(sys.argv)

    api_key = config.get("deepl_api_key", "")
    if not isinstance(api_key, str) or not api_key.strip():
        QMessageBox.warning(
            None,
            "DeepL API key is missing",
            "Откройте config.json, вставьте ключ в поле deepl_api_key "
            "и перезапустите приложение.",
        )
        return 1

    try:
        translator = Translator(api_key.strip())
    except Exception as error:
        print(f"Failed to initialize DeepL translator: {error}", file=sys.stderr)
        QMessageBox.critical(
            None,
            "DeepL initialization error",
            "Не удалось инициализировать DeepL. Проверьте deepl_api_key "
            "в config.json.",
        )
        return 1

    overlay = OverlayWindow(
        max_messages=max(1, _config_int(config, "max_messages", 8)),
        font_size=max(1, _config_int(config, "font_size", 14)),
    )
    overlay_width = _config_int(config, "overlay_width", 0)
    overlay_height = _config_int(config, "overlay_height", 0)
    if overlay_width > 0 and overlay_height > 0:
        overlay.set_size(overlay_width, overlay_height)
    overlay_x = _config_int(config, "overlay_x", DEFAULT_OVERLAY_POSITION)
    overlay_y = _config_int(config, "overlay_y", DEFAULT_OVERLAY_POSITION)
    overlay.move_to(overlay_x, overlay_y)

    inventory_overlay = InventoryOverlay()
    inventory_width = _config_int(config, "inventory_overlay_width", 0)
    inventory_height = _config_int(config, "inventory_overlay_height", 0)
    if inventory_width > 0 and inventory_height > 0:
        inventory_overlay.set_size(inventory_width, inventory_height)
    inventory_overlay.set_filters(
        str(config.get("inventory_team_filter", "all")),
        _config_float(config, "inventory_min_price", 0.0),
    )
    inv_x = _config_int(
        config,
        "inventory_overlay_x",
        DEFAULT_INVENTORY_OVERLAY_X,
    )
    inv_y = _config_int(
        config,
        "inventory_overlay_y",
        DEFAULT_INVENTORY_OVERLAY_Y,
    )
    inventory_overlay.move_to(inv_x, inv_y)

    screen = QGuiApplication.screenAt(
        QPoint(overlay_x, overlay_y)
    ) or QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        clamped_x = max(
            available.x(),
            min(overlay_x, available.right() - overlay.width()),
        )
        clamped_y = max(
            available.y(),
            min(overlay_y, available.bottom() - overlay.height()),
        )
        overlay.move_to(clamped_x, clamped_y)

    screen = QGuiApplication.screenAt(
        QPoint(inv_x, inv_y)
    ) or QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        clamped_inv_x = max(
            available.x(),
            min(inv_x, available.right() - inventory_overlay.width()),
        )
        clamped_inv_y = max(
            available.y(),
            min(inv_y, available.bottom() - inventory_overlay.height()),
        )
        inventory_overlay.move_to(clamped_inv_x, clamped_inv_y)

    try:
        from ui.input_window import InputWindow
    except (ImportError, ModuleNotFoundError) as error:
        print(f"Failed to import InputWindow: {error}", file=sys.stderr)
        QMessageBox.critical(
            None,
            "InputWindow is missing",
            "Не найден модуль ui/input_window.py. Добавьте InputWindow "
            "перед запуском приложения.",
        )
        return 1

    input_window = InputWindow(translator, config)
    initial_chat_mode = config.get("chat_mode", "all")
    if (
        not isinstance(initial_chat_mode, str)
        or initial_chat_mode not in VALID_CHAT_MODES
    ):
        initial_chat_mode = "all"
        config["chat_mode"] = initial_chat_mode
    input_window.set_chat_mode(initial_chat_mode)

    chat_sender = ChatSender()
    hotkey_manager = HotkeyManager(config)

    controller: ApplicationController
    def on_log_message(message: ChatMessage) -> None:
        controller.on_log_message(message)

    def on_log_line(line: str) -> None:
        controller.on_log_line(line)

    log_path = config.get("log_path", "")
    log_watcher = LogWatcher(
        str(log_path), on_log_message, on_line=on_log_line
    )

    controller = ApplicationController(
        config=config,
        translator=translator,
        overlay=overlay,
        inventory_overlay=inventory_overlay,
        input_window=input_window,
        chat_sender=chat_sender,
        log_watcher=log_watcher,
        hotkey_manager=hotkey_manager,
    )
    input_window.send_message.connect(controller.send_chat_message)

    hotkey_manager.register(
        "toggle_drag",
        lambda: controller.request_ui_action("toggle_drag"),
    )
    hotkey_manager.register(
        "input_window",
        lambda: controller.request_ui_action("toggle_input_window"),
    )
    hotkey_manager.register(
        "toggle_chat_mode",
        lambda: controller.request_ui_action("toggle_chat_mode"),
    )

    app.aboutToQuit.connect(controller.shutdown)

    log_watcher.start()
    hotkey_manager.start()
    overlay.show()
    inventory_overlay.show()
    controller.sync_overlay_visibility()
    controller.start_inventory_services()

    try:
        return app.exec()
    finally:
        controller.shutdown()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Fatal error: {error}", file=sys.stderr)
        raise
