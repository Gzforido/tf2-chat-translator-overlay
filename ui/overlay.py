"""Прозрачный оверлей с последними переводами чата Team Fortress 2."""

import ctypes
import html
import sys
import time
from pathlib import Path
from typing import Optional

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QPoint, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QMouseEvent, QShowEvent
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.log_parser import ChatMessage


OVERLAY_WIDTH = 520

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000

HWND_TOPMOST = -1

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


class OverlayWindow(QWidget):
    """Borderless click-through окно с переведёнными сообщениями чата."""

    _message_queued = pyqtSignal(object, str)

    def __init__(self, max_messages: int = 8, font_size: int = 14) -> None:
        super().__init__()
        self._message_queued.connect(self._add_message_safe)

        self._max_messages = min(100, max(1, max_messages))
        self._font_size = max(1, font_size)
        self._drag_enabled = False
        self._drag_offset: Optional[QPoint] = None
        self._resize_origin: Optional[QPoint] = None
        self._resize_start: Optional[tuple[int, int]] = None
        self._manual_size = False
        self._message_widgets: list[QWidget] = []

        self.setObjectName("overlayWindow")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumSize(OVERLAY_WIDTH, 80)
        self.resize(OVERLAY_WIDTH, 80)

        self.setStyleSheet(
            """
            QWidget#overlayWindow {
                background-color: rgba(0, 0, 0, 160);
                border-radius: 8px;
            }
            QWidget#messageBlock {
                background-color: transparent;
            }
            QScrollArea, QWidget#messageContainer {
                background-color: transparent;
                border: none;
            }
            QLabel#originalText {
                background-color: transparent;
                color: #AAAAAA;
            }
            QLabel#translatedText {
                background-color: transparent;
                color: #FFFFFF;
            }
            """
        )

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(0)
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._message_container = QWidget(self._scroll_area)
        self._message_container.setObjectName("messageContainer")
        self._message_layout = QVBoxLayout(self._message_container)
        self._message_layout.setContentsMargins(0, 0, 0, 0)
        self._message_layout.setSpacing(8)
        self._message_layout.addStretch(1)
        self._scroll_area.setWidget(self._message_container)
        self._layout.addWidget(self._scroll_area)
        self._resize_hint = QLabel("◢", self)
        self._resize_hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._resize_hint.setStyleSheet("color: #8E9BA4; font-size: 12px;")
        self._resize_hint.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._resize_hint.setVisible(False)
        self._layout.addWidget(self._resize_hint)

    def add_message(self, msg: ChatMessage, translation: str) -> None:
        """Безопасно передать сообщение в поток, которому принадлежит окно."""
        if QThread.currentThread() is not self.thread():
            self._message_queued.emit(msg, translation)
            return

        self._add_message_safe(msg, translation)

    @pyqtSlot(object, str)
    def _add_message_safe(self, msg: object, translation: str) -> None:
        """Добавить переведённое сообщение и удалить самое старое при лимите."""
        message_widget = self._create_message_widget(msg, translation)
        self._message_widgets.append(message_widget)
        self._message_layout.addWidget(message_widget)

        while len(self._message_widgets) > self._max_messages:
            oldest_widget = self._message_widgets.pop(0)
            self._message_layout.removeWidget(oldest_widget)
            oldest_widget.deleteLater()

        self._resize_to_content()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def clear_messages(self) -> None:
        """Удалить все сообщения из оверлея."""
        for message_widget in self._message_widgets:
            self._message_layout.removeWidget(message_widget)
            message_widget.deleteLater()

        self._message_widgets.clear()
        self._resize_to_content()

    def set_drag_enabled(self, enabled: bool) -> None:
        """Разрешить перетаскивание или вернуть режим click-through."""
        self._drag_enabled = enabled
        self._drag_offset = None
        self._resize_origin = None
        self._resize_hint.setVisible(enabled)

        if self.isVisible():
            self._apply_windows_input_style()

    def move_to(self, x: int, y: int) -> None:
        """Переместить оверлей в экранные координаты ``x`` и ``y``."""
        self.move(x, y)

    def get_position(self) -> tuple[int, int]:
        """Вернуть текущую позицию оверлея."""
        return self.x(), self.y()

    def set_size(self, width: int, height: int) -> None:
        """Восстановить пользовательский размер окна."""
        self._manual_size = True
        self.setMinimumWidth(300)
        self._scroll_area.setMinimumHeight(40)
        self._scroll_area.setMaximumHeight(16_777_215)
        self.resize(max(self.minimumWidth(), width), max(self.minimumHeight(), height))

    def get_size(self) -> tuple[int, int]:
        return self.width(), self.height()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._drag_enabled and event.button() == Qt.MouseButton.LeftButton:
            if self._is_resize_corner(event.position().toPoint()):
                self._manual_size = True
                self.setMinimumWidth(300)
                self._resize_origin = event.globalPosition().toPoint()
                self._resize_start = self.get_size()
                event.accept()
                return
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
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

    def _create_message_widget(
        self,
        msg: ChatMessage,
        translation: str,
    ) -> QWidget:
        message_widget = QWidget(self)
        message_widget.setObjectName("messageBlock")
        message_widget.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )

        message_layout = QVBoxLayout(message_widget)
        message_layout.setContentsMargins(0, 0, 0, 0)
        message_layout.setSpacing(1)

        player_name = html.escape(msg.player_name)
        original_text = html.escape(msg.text)
        translated_text = html.escape(translation)

        team_marker = (
            ' <span style="color: #4FC3F7;">[TEAM]</span>'
            if msg.is_team
            else ""
        )

        original_label = QLabel(
            f"[{player_name}]{team_marker}: {original_text}",
            message_widget,
        )
        original_label.setObjectName("originalText")
        original_label.setTextFormat(Qt.TextFormat.RichText)
        original_label.setWordWrap(True)
        original_label.setStyleSheet(
            f"font-size: {max(1, self._font_size - 1)}px;"
        )

        translated_label = QLabel(
            f"→ {translated_text}",
            message_widget,
        )
        translated_label.setObjectName("translatedText")
        translated_label.setTextFormat(Qt.TextFormat.RichText)
        translated_label.setWordWrap(True)
        translated_label.setStyleSheet(
            f"font-size: {self._font_size}px; font-weight: 700;"
        )

        message_layout.addWidget(original_label)
        message_layout.addWidget(translated_label)
        return message_widget

    def _resize_to_content(self) -> None:
        if self._manual_size:
            return
        self._message_layout.invalidate()
        self._message_layout.activate()
        content_width = max(1, self.width() - 24)
        content_height = sum(
            widget.heightForWidth(content_width)
            if widget.hasHeightForWidth()
            else widget.sizeHint().height()
            for widget in self._message_widgets
        ) + max(0, len(self._message_widgets) - 1) * self._message_layout.spacing()
        screen = self.screen() or QApplication.primaryScreen()
        max_height = (
            max(80, min(400, screen.availableGeometry().height() - 100))
            if screen is not None
            else 400
        )
        self._scroll_area.setFixedHeight(max(40, min(content_height, max_height)))
        self._layout.invalidate()
        self._layout.activate()
        self.adjustSize()
        self._clamp_to_screen()

    def _scroll_to_bottom(self) -> None:
        scrollbar = self._scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

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

            flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_FRAMECHANGED
            if not set_window_pos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as error:
            print(f"Failed to update overlay click-through: {error}", file=sys.stderr)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay = OverlayWindow()
    overlay.move_to(50, 50)

    demo_messages = (
        (
            ChatMessage("Scout", "Medic!", False, time.time()),
            "Медик!",
        ),
        (
            ChatMessage("Heavy Weapons Guy", "Push the cart!", True, time.time()),
            "Толкайте вагонетку!",
        ),
        (
            ChatMessage("[Clan] Spy", "They have a sentry ahead.", True, time.time()),
            "Впереди у них турель.",
        ),
    )

    for demo_message, demo_translation in demo_messages:
        overlay.add_message(demo_message, demo_translation)

    # В демонстрационном режиме окно можно сразу перетаскивать мышью.
    overlay.set_drag_enabled(True)
    overlay.show()
    sys.exit(app.exec())
