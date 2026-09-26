"""Прозрачный оверлей со стоимостью инвентарей игроков TF2."""

import ctypes
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QPoint, QThread, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QDesktopServices,
    QDoubleValidator,
    QMouseEvent,
    QShowEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.inventory_scanner import PlayerInventory


OVERLAY_WIDTH = 320
MAX_SCROLL_HEIGHT = 340
ROW_HEIGHT = 32
BACKPACK_PROFILE_URL = "https://backpack.tf/profiles/{steamid}"
STEAMID64_BASE = 76_561_197_960_265_728
STEAMID64_MAX = STEAMID64_BASE + (2**32 - 1)

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
HWND_TOPMOST = -1

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


@dataclass
class _PlayerState:
    steamid: str
    player_name: str
    team: str
    status: str
    total_value_usd: Optional[float] = None
    market_value_usd: Optional[float] = None
    item_count: int = 0


@dataclass
class _PlayerRowWidgets:
    widget: QWidget
    team_dot: QLabel
    name_label: QLabel
    value_label: QLabel
    profile_button: QPushButton


class InventoryOverlay(QWidget):
    """Компактная click-through таблица инвентарей игроков."""

    refresh_requested = pyqtSignal()

    _update_queued = pyqtSignal(object)
    _remove_queued = pyqtSignal(str)
    _clear_queued = pyqtSignal()
    _loading_queued = pyqtSignal(str, str, str)
    _private_queued = pyqtSignal(str, str, str)
    _local_team_queued = pyqtSignal(str)
    _player_teams_queued = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()

        self._update_queued.connect(self._update_player_safe)
        self._remove_queued.connect(self._remove_player_safe)
        self._clear_queued.connect(self._clear_players_safe)
        self._loading_queued.connect(self._set_player_loading_safe)
        self._private_queued.connect(self._set_player_private_safe)
        self._local_team_queued.connect(self._set_local_team_safe)
        self._player_teams_queued.connect(self._set_player_teams_safe)

        self._players: dict[str, _PlayerState] = {}
        self._row_widgets: dict[str, _PlayerRowWidgets] = {}
        self._roster_teams: dict[str, str] = {}
        self._local_team = ""
        self._drag_enabled = False
        self._drag_offset: Optional[QPoint] = None
        self._resize_origin: Optional[QPoint] = None
        self._resize_start: Optional[tuple[int, int]] = None
        self._manual_size = False

        self.setObjectName("inventoryOverlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(260, 120)
        self.resize(OVERLAY_WIDTH, 150)

        self.setStyleSheet(
            """
            QWidget#inventoryOverlay {
                background-color: rgba(0, 0, 0, 180);
                border-radius: 8px;
            }
            QLabel#inventoryTitle {
                color: #FFFFFF;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton#refreshButton {
                background-color: rgba(255, 255, 255, 28);
                border: 1px solid rgba(255, 255, 255, 45);
                border-radius: 4px;
                color: #FFFFFF;
                font-size: 16px;
                min-width: 28px;
                max-width: 28px;
                min-height: 24px;
                max-height: 24px;
            }
            QPushButton#refreshButton:hover {
                background-color: rgba(79, 195, 247, 80);
            }
            QPushButton#profileButton {
                background-color: transparent;
                border: none;
                border-radius: 3px;
                color: #4FC3F7;
                font-size: 13px;
                font-weight: 700;
                min-width: 20px;
                max-width: 20px;
                min-height: 20px;
                max-height: 20px;
                padding: 0;
            }
            QPushButton#profileButton:hover {
                background-color: rgba(79, 195, 247, 55);
                color: #FFFFFF;
            }
            QComboBox, QLineEdit {
                background-color: rgba(255, 255, 255, 22);
                border: 1px solid rgba(255, 255, 255, 42);
                border-radius: 4px;
                color: #EEEEEE;
                min-height: 24px;
                padding: 1px 6px;
            }
            QComboBox QAbstractItemView {
                background-color: #202020;
                color: #EEEEEE;
                selection-background-color: #345A6B;
            }
            QScrollArea, QWidget#playersContainer {
                background-color: transparent;
                border: none;
            }
            QWidget#playerRow {
                background-color: rgba(8, 12, 18, 235);
                border: 1px solid rgba(255, 255, 255, 35);
                border-radius: 4px;
            }
            QLabel#emptyPlayers {
                color: #777777;
                font-style: italic;
                padding: 8px;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 7px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 70);
                border-radius: 3px;
                min-height: 24px;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
            }
            """
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 8, 10, 9)
        root_layout.setSpacing(7)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(1, 0, 0, 0)
        title_label = QLabel("ИНВЕНТАРИ", self)
        title_label.setObjectName("inventoryTitle")
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)

        self._refresh_button = QPushButton("↻", self)
        self._refresh_button.setObjectName("refreshButton")
        self._refresh_button.setToolTip("Обновить инвентари")
        self._refresh_button.clicked.connect(self.refresh_requested.emit)
        header_layout.addWidget(self._refresh_button)
        root_layout.addLayout(header_layout)

        filters_layout = QHBoxLayout()
        filters_layout.setContentsMargins(0, 0, 0, 0)
        filters_layout.setSpacing(6)

        self._team_filter = QComboBox(self)
        self._team_filter.addItem("Все", "all")
        self._team_filter.addItem("Союзники", "allies")
        self._team_filter.addItem("Противники", "enemies")
        self._team_filter.currentIndexChanged.connect(self.apply_filters)
        filters_layout.addWidget(self._team_filter, 1)

        self._minimum_price = QLineEdit(self)
        self._minimum_price.setPlaceholderText("$0")
        self._minimum_price.setValidator(
            QDoubleValidator(0.0, 999_999_999.0, 2, self)
        )
        self._minimum_price.setMaximumWidth(92)
        self._minimum_price.textChanged.connect(self.apply_filters)
        filters_layout.addWidget(self._minimum_price)
        root_layout.addLayout(filters_layout)

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        self._players_container = QWidget(self._scroll_area)
        self._players_container.setObjectName("playersContainer")
        self._players_layout = QVBoxLayout(self._players_container)
        self._players_layout.setContentsMargins(0, 0, 0, 0)
        self._players_layout.setSpacing(4)

        self._empty_label = QLabel("Нет данных", self._players_container)
        self._empty_label.setObjectName("emptyPlayers")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._players_layout.addWidget(self._empty_label)
        self._players_layout.addStretch(1)

        self._scroll_area.setWidget(self._players_container)
        root_layout.addWidget(self._scroll_area)
        self._resize_hint = QLabel("◢", self)
        self._resize_hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._resize_hint.setStyleSheet("color: #8E9BA4; font-size: 12px;")
        self._resize_hint.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._resize_hint.setVisible(False)
        root_layout.addWidget(self._resize_hint)
        self._resize_to_visible_rows(0)

    def update_player(self, inventory: PlayerInventory) -> None:
        """Потокобезопасно обновить оценённый инвентарь игрока."""
        if QThread.currentThread() is not self.thread():
            self._update_queued.emit(inventory)
            return
        self._update_player_safe(inventory)

    def remove_player(self, steamid: str) -> None:
        """Потокобезопасно удалить игрока по SteamID64."""
        normalized_steamid = str(steamid)
        if QThread.currentThread() is not self.thread():
            self._remove_queued.emit(normalized_steamid)
            return
        self._remove_player_safe(normalized_steamid)

    def clear_players(self) -> None:
        """Потокобезопасно очистить таблицу игроков."""
        if QThread.currentThread() is not self.thread():
            self._clear_queued.emit()
            return
        self._clear_players_safe()

    def set_player_loading(
        self,
        steamid: str,
        player_name: str,
        team: str,
    ) -> None:
        """Добавить необязательное состояние «загрузка...» для интеграции."""
        values = (str(steamid), str(player_name), str(team))
        if QThread.currentThread() is not self.thread():
            self._loading_queued.emit(*values)
            return
        self._set_player_loading_safe(*values)

    def set_player_private(
        self,
        steamid: str,
        player_name: str,
        team: str,
    ) -> None:
        """Добавить необязательное состояние закрытого инвентаря."""
        values = (str(steamid), str(player_name), str(team))
        if QThread.currentThread() is not self.thread():
            self._private_queued.emit(*values)
            return
        self._set_player_private_safe(*values)

    def set_local_team(self, team: str) -> None:
        """Задать команду пользователя для фильтров союзников/противников."""
        normalized_team = self._normalize_team(team)
        if QThread.currentThread() is not self.thread():
            self._local_team_queued.emit(normalized_team)
            return
        self._set_local_team_safe(normalized_team)

    def set_player_teams(self, teams: dict[str, str]) -> None:
        """Применить команды из текущего RCON-состава независимо от цен."""
        normalized_teams = {
            str(steamid): self._normalize_team(team)
            for steamid, team in teams.items()
        }
        if QThread.currentThread() is not self.thread():
            self._player_teams_queued.emit(normalized_teams)
            return
        self._set_player_teams_safe(normalized_teams)

    def set_drag_enabled(self, enabled: bool) -> None:
        """Разрешить перетаскивание или вернуть click-through режим."""
        self._drag_enabled = bool(enabled)
        self._drag_offset = None
        self._resize_origin = None
        self._resize_hint.setVisible(self._drag_enabled)
        if self.isVisible():
            self._apply_windows_input_style()

    def move_to(self, x: int, y: int) -> None:
        """Переместить окно в экранные координаты."""
        self.move(x, y)

    def get_position(self) -> tuple[int, int]:
        """Вернуть текущие экранные координаты окна."""
        return self.x(), self.y()

    def set_size(self, width: int, height: int) -> None:
        """Восстановить пользовательский размер окна."""
        self._manual_size = True
        self._scroll_area.setMinimumHeight(40)
        self._scroll_area.setMaximumHeight(16_777_215)
        self.resize(max(self.minimumWidth(), width), max(self.minimumHeight(), height))

    def get_size(self) -> tuple[int, int]:
        return self.width(), self.height()

    def open_backpack_profile(self, steamid: str) -> bool:
        """Открыть страницу оценки инвентаря игрока на backpack.tf."""
        normalized_steamid = str(steamid).strip()
        try:
            numeric_steamid = int(normalized_steamid)
        except (TypeError, ValueError):
            numeric_steamid = 0

        if not STEAMID64_BASE <= numeric_steamid <= STEAMID64_MAX:
            print(
                f"InventoryOverlay: invalid SteamID64 {steamid!r}",
                file=sys.stderr,
            )
            return False

        profile_url = QUrl(
            BACKPACK_PROFILE_URL.format(steamid=normalized_steamid)
        )
        if QDesktopServices.openUrl(profile_url):
            return True

        print(
            f"InventoryOverlay: failed to open {profile_url.toString()}",
            file=sys.stderr,
        )
        return False

    def set_filters(self, team_filter: str, minimum_price: float) -> None:
        index = self._team_filter.findData(team_filter)
        if index >= 0:
            self._team_filter.setCurrentIndex(index)
        text = f"{minimum_price:.2f}".rstrip("0").rstrip(".")
        self._minimum_price.setText(text if minimum_price > 0 else "")
        self.apply_filters()

    def get_filters(self) -> tuple[str, float]:
        team = self._team_filter.currentData() or "all"
        return team, self._parse_minimum_price()

    @pyqtSlot()
    def apply_filters(self) -> None:
        """Мгновенно применить фильтр команды и минимальной стоимости."""
        filter_mode = self._team_filter.currentData()
        minimum_price = self._parse_minimum_price()
        visible_count = 0

        for steamid, row in self._row_widgets.items():
            player = self._players[steamid]
            price_visible = (
                minimum_price <= 0
                or (
                    player.total_value_usd is not None
                    and player.total_value_usd >= minimum_price
                )
            )
            visible = (
                self._matches_team_filter(player.team, filter_mode)
                and price_visible
            )
            row.widget.setVisible(visible)
            if not visible:
                continue

            visible_count += 1
            self._style_row(row, player, below_minimum=False)

        if visible_count == 0 and filter_mode != "all" and not self._local_team:
            self._empty_label.setText("Команда неизвестна")
        else:
            self._empty_label.setText("Нет данных")
        self._empty_label.setVisible(visible_count == 0)
        self._resize_to_visible_rows(visible_count)

    @pyqtSlot(object)
    def _update_player_safe(self, inventory: object) -> None:
        if not isinstance(inventory, PlayerInventory):
            print("InventoryOverlay: invalid PlayerInventory", file=sys.stderr)
            return

        valid_statuses = {"ready", "private", "api_error", "prices_unavailable"}
        status = (
            inventory.status
            if inventory.status in valid_statuses
            else "api_error"
        )

        value: Optional[float] = None
        market_value: Optional[float] = None
        if status == "ready":
            if inventory.total_value_usd is None:
                status = "api_error"
            else:
                try:
                    parsed_value = float(inventory.total_value_usd)
                except (TypeError, ValueError):
                    status = "api_error"
                else:
                    if math.isfinite(parsed_value):
                        value = max(0.0, parsed_value)
                    else:
                        status = "api_error"

            if inventory.market_value_usd is not None:
                try:
                    parsed_market_value = float(inventory.market_value_usd)
                except (TypeError, ValueError):
                    pass
                else:
                    if math.isfinite(parsed_market_value):
                        market_value = max(0.0, parsed_market_value)

        steamid = str(inventory.steamid)
        self._players[steamid] = _PlayerState(
            steamid=steamid,
            player_name=str(inventory.player_name),
            team=self._roster_teams.get(
                steamid, self._normalize_team(inventory.team)
            ),
            status=status,
            total_value_usd=value,
            market_value_usd=market_value,
            item_count=max(0, int(inventory.item_count)),
        )
        self._rebuild_rows()

    @pyqtSlot(str)
    def _remove_player_safe(self, steamid: str) -> None:
        self._players.pop(steamid, None)
        self._roster_teams.pop(steamid, None)
        self._rebuild_rows()

    @pyqtSlot()
    def _clear_players_safe(self) -> None:
        self._players.clear()
        self._roster_teams.clear()
        self._rebuild_rows()

    @pyqtSlot(str, str, str)
    def _set_player_loading_safe(
        self,
        steamid: str,
        player_name: str,
        team: str,
    ) -> None:
        self._players[steamid] = _PlayerState(
            steamid=steamid,
            player_name=player_name,
            team=self._roster_teams.get(steamid) or self._normalize_team(team),
            status="loading",
        )
        self._rebuild_rows()

    @pyqtSlot(str, str, str)
    def _set_player_private_safe(
        self,
        steamid: str,
        player_name: str,
        team: str,
    ) -> None:
        self._players[steamid] = _PlayerState(
            steamid=steamid,
            player_name=player_name,
            team=self._roster_teams.get(steamid) or self._normalize_team(team),
            status="private",
        )
        self._rebuild_rows()

    @pyqtSlot(str)
    def _set_local_team_safe(self, team: str) -> None:
        self._local_team = self._normalize_team(team)
        self.apply_filters()

    @pyqtSlot(object)
    def _set_player_teams_safe(self, teams: object) -> None:
        if not isinstance(teams, dict):
            return
        self._roster_teams = {
            str(steamid): self._normalize_team(team)
            for steamid, team in teams.items()
        }
        for steamid, player in self._players.items():
            player.team = self._roster_teams.get(steamid, "")
            row = self._row_widgets.get(steamid)
            if row is not None:
                row.team_dot.setText(self._team_dot(player.team))
        self.apply_filters()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._drag_enabled and event.button() == Qt.MouseButton.LeftButton:
            if self._is_resize_corner(event.position().toPoint()):
                self._manual_size = True
                self._scroll_area.setMinimumHeight(40)
                self._scroll_area.setMaximumHeight(16_777_215)
                self._resize_origin = event.globalPosition().toPoint()
                self._resize_start = self.get_size()
                event.accept()
                return
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
            )
            event.accept()
            return
        event.ignore()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._drag_enabled
            and self._resize_origin is not None
            and self._resize_start is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            delta = event.globalPosition().toPoint() - self._resize_origin
            self.resize(
                max(self.minimumWidth(), self._resize_start[0] + delta.x()),
                max(self.minimumHeight(), self._resize_start[1] + delta.y()),
            )
            event.accept()
            return
        if (
            self._drag_enabled
            and self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        event.ignore()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self._resize_origin = None
            self._resize_start = None
        super().mouseReleaseEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_windows_input_style()

    def _rebuild_rows(self) -> None:
        for row in self._row_widgets.values():
            self._players_layout.removeWidget(row.widget)
            row.widget.deleteLater()
        self._row_widgets.clear()

        sorted_players = sorted(
            self._players.values(),
            key=lambda player: (
                player.total_value_usd is not None,
                player.total_value_usd
                if player.total_value_usd is not None
                else -1.0,
                player.player_name.casefold(),
            ),
            reverse=True,
        )

        for index, player in enumerate(sorted_players):
            row = self._create_player_row(player)
            self._row_widgets[player.steamid] = row
            self._players_layout.insertWidget(index, row.widget)

        self.apply_filters()

    def _create_player_row(self, player: _PlayerState) -> _PlayerRowWidgets:
        row_widget = QWidget(self._players_container)
        row_widget.setObjectName("playerRow")
        row_widget.setFixedHeight(ROW_HEIGHT)

        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(6, 2, 6, 2)
        row_layout.setSpacing(5)

        team_dot = QLabel(self._team_dot(player.team), row_widget)
        team_dot.setFixedWidth(17)
        team_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row_layout.addWidget(team_dot)

        name_label = QLabel(
            self._truncate_name(player.player_name),
            row_widget,
        )
        name_label.setToolTip(player.player_name)
        row_layout.addWidget(name_label, 1)

        value_label = QLabel(row_widget)
        value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row_layout.addWidget(value_label)

        profile_button = QPushButton("↗", row_widget)
        profile_button.setObjectName("profileButton")
        profile_button.setCursor(Qt.CursorShape.PointingHandCursor)
        profile_button.setToolTip(
            "Открыть оценку инвентаря на backpack.tf "
            "(сначала включите режим управления клавишей INS)"
        )
        profile_button.clicked.connect(
            lambda _checked=False, steamid=player.steamid: (
                self.open_backpack_profile(steamid)
            )
        )
        row_layout.addWidget(profile_button)

        row = _PlayerRowWidgets(
            widget=row_widget,
            team_dot=team_dot,
            name_label=name_label,
            value_label=value_label,
            profile_button=profile_button,
        )
        self._style_row(row, player, below_minimum=False)
        return row

    @staticmethod
    def _style_row(
        row: _PlayerRowWidgets,
        player: _PlayerState,
        below_minimum: bool,
    ) -> None:
        text_color = "#777777" if below_minimum else "#EEEEEE"
        value_color = "#777777" if below_minimum else "#FFFFFF"
        value_style = f"color: {value_color}; font-weight: 700;"

        if player.status == "loading":
            value_text = "загрузка..."
            value_style = "color: #AAAAAA; font-style: italic;"
        elif player.status == "private":
            value_text = "[приватный]"
            value_style = "color: #999999;"
        elif player.status == "api_error":
            value_text = "[ошибка API]"
            value_style = "color: #EF9A9A;"
        elif player.status == "prices_unavailable":
            value_text = "[цены недоступны]"
            value_style = "color: #FFCC80;"
        else:
            value = player.total_value_usd or 0.0
            value_text = f"C ${value:,.0f}"
            if player.market_value_usd is not None:
                value_text += f" · S ${player.market_value_usd:,.0f}"
            else:
                value_text += " · S —"

        row.name_label.setStyleSheet(f"color: {text_color};")
        row.value_label.setText(value_text)
        row.value_label.setStyleSheet(value_style)
        if player.status == "ready":
            tooltip = f"Community value: ${value:,.2f}"
            if player.market_value_usd is not None:
                tooltip += (
                    f"\nSteam Market value: ${player.market_value_usd:,.2f}"
                )
            row.value_label.setToolTip(tooltip)
        else:
            row.value_label.setToolTip("")

    def _matches_team_filter(self, team: str, filter_mode: object) -> bool:
        if filter_mode == "all":
            return True
        if not self._local_team or not team:
            return False
        if filter_mode == "allies":
            return team == self._local_team
        if filter_mode == "enemies":
            return bool(team) and team != self._local_team
        return True

    def _parse_minimum_price(self) -> float:
        text = self._minimum_price.text().strip().replace(",", ".")
        if not text:
            return 0.0
        try:
            value = float(text)
        except ValueError:
            return 0.0
        if not math.isfinite(value):
            return 0.0
        return max(0.0, value)

    def _resize_to_visible_rows(self, visible_count: int) -> None:
        if self._manual_size:
            return
        content_rows = max(1, visible_count)
        content_height = content_rows * ROW_HEIGHT + max(
            0,
            content_rows - 1,
        ) * self._players_layout.spacing()
        self._scroll_area.setFixedHeight(
            min(MAX_SCROLL_HEIGHT, content_height + 4)
        )
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()
        self._clamp_to_screen()

    def _is_resize_corner(self, point: QPoint) -> bool:
        return point.x() >= self.width() - 24 and point.y() >= self.height() - 24

    def _clamp_to_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return

        available = screen.availableGeometry()
        max_x = available.x() + max(0, available.width() - self.width())
        max_y = available.y() + max(0, available.height() - self.height())
        self.move(
            max(available.x(), min(self.x(), max_x)),
            max(available.y(), min(self.y(), max_y)),
        )

    @staticmethod
    def _truncate_name(player_name: str) -> str:
        return (
            player_name
            if len(player_name) <= 18
            else f"{player_name[:17]}…"
        )

    @staticmethod
    def _normalize_team(team: object) -> str:
        normalized_team = str(team).strip().casefold()
        if normalized_team == "red":
            return "Red"
        if normalized_team == "blue":
            return "Blue"
        if normalized_team == "defenders":
            return "Defenders"
        if normalized_team == "invaders":
            return "Invaders"
        return ""

    @staticmethod
    def _team_dot(team: str) -> str:
        if team == "Red":
            return "🔴"
        if team == "Blue":
            return "🔵"
        if team == "Defenders":
            return "🛡"
        if team == "Invaders":
            return "⚔"
        return "⚪"

    def _apply_windows_input_style(self) -> None:
        if sys.platform != "win32":
            return

        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = int(self.winId())

            get_window_long = user32.GetWindowLongW
            get_window_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_window_long.restype = ctypes.c_long

            set_window_long = user32.SetWindowLongW
            set_window_long.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_long,
            ]
            set_window_long.restype = ctypes.c_long

            set_window_pos = user32.SetWindowPos
            set_window_pos.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            set_window_pos.restype = ctypes.c_int

            ctypes.set_last_error(0)
            extended_style = get_window_long(hwnd, GWL_EXSTYLE)
            get_style_error = ctypes.get_last_error()
            if extended_style == 0 and get_style_error != 0:
                raise ctypes.WinError(get_style_error)

            extended_style |= WS_EX_LAYERED
            if self._drag_enabled:
                extended_style &= ~WS_EX_TRANSPARENT
            else:
                extended_style |= WS_EX_TRANSPARENT

            ctypes.set_last_error(0)
            previous_style = set_window_long(
                hwnd,
                GWL_EXSTYLE,
                extended_style,
            )
            if previous_style == 0 and ctypes.get_last_error() != 0:
                raise ctypes.WinError(ctypes.get_last_error())

            flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            if not set_window_pos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                flags | SWP_FRAMECHANGED,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as error:
            print(
                f"Failed to update inventory overlay style: {error}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay = InventoryOverlay()
    overlay.move_to(600, 80)
    overlay.set_local_team("Red")
    overlay.update_player(
        PlayerInventory(
            steamid="demo-player-1",
            player_name="Red Inventory King",
            team="Red",
            total_value_usd=1234.0,
            item_count=412,
            fetched_at=time.time(),
        )
    )
    overlay.update_player(
        PlayerInventory(
            steamid="demo-player-2",
            player_name="Blue Scout",
            team="Blue",
            total_value_usd=86.0,
            item_count=77,
            fetched_at=time.time(),
        )
    )
    overlay.set_player_loading(
        "demo-player-3",
        "Loading Player",
        "Blue",
    )
    overlay.set_player_private(
        "demo-player-4",
        "Private Backpack",
        "Red",
    )
    overlay.set_drag_enabled(True)
    overlay.show()
    sys.exit(app.exec())
