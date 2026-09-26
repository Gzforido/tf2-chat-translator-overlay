"""Всплывающее окно обратного перевода для чата Team Fortress 2."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QEvent, QPoint, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFocusEvent, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from core.translator import Translator


DEFAULT_REVERSE_TARGET_LANGUAGE = "EN-US"
SUPPORTED_TARGET_LANGUAGES = {"RU", "EN-US", "ZH"}
VALID_CHAT_MODES = {"all", "team"}
TRANSLATION_ERROR = "[Translation error]"
DEBOUNCE_INTERVAL_MS = 300


class InputWindow(QWidget):
    """Компактное окно ввода с живым обратным переводом."""

    send_message = pyqtSignal(str, str)
    _translation_ready = pyqtSignal(int, str)

    def __init__(self, translator: Translator, config: dict) -> None:
        super().__init__()

        self._translator = translator
        self._config = config
        self._translated_text = ""
        self._translation_request_id = 0
        self._drag_offset: Optional[QPoint] = None
        self._language_buttons: dict[str, QPushButton] = {}

        configured_language = config.get(
            "reverse_target_lang",
            DEFAULT_REVERSE_TARGET_LANGUAGE,
        )
        if (
            not isinstance(configured_language, str)
            or configured_language not in SUPPORTED_TARGET_LANGUAGES
        ):
            configured_language = DEFAULT_REVERSE_TARGET_LANGUAGE
        self._target_lang = configured_language
        self._config["reverse_target_lang"] = configured_language

        configured_chat_mode = config.get("chat_mode", "all")
        if (
            not isinstance(configured_chat_mode, str)
            or configured_chat_mode not in VALID_CHAT_MODES
        ):
            configured_chat_mode = "all"
        self._chat_mode = configured_chat_mode
        self._config["chat_mode"] = configured_chat_mode

        self._configure_window()
        self._create_interface()
        self._connect_signals()
        self._update_language_buttons()
        self._update_chat_mode_indicator()

    def show(self) -> None:
        """Показать окно и передать фокус полю ввода."""
        super().show()
        self.raise_()
        self.activateWindow()
        self._input_field.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def hide(self) -> None:
        """Скрыть окно и сбросить текущий ввод с переводом."""
        self._reset_input()
        super().hide()

    def set_chat_mode(self, mode: str) -> None:
        """Переключить режим отправки между общим и командным чатом."""
        normalized_mode = mode.lower()
        if normalized_mode not in VALID_CHAT_MODES:
            raise ValueError("chat mode must be 'all' or 'team'")

        self._chat_mode = normalized_mode
        self._config["chat_mode"] = normalized_mode
        self._update_chat_mode_indicator()

    def get_chat_mode(self) -> str:
        """Вернуть текущий режим отправки сообщения."""
        return self._chat_mode

    def set_target_lang(self, lang: str) -> None:
        """Изменить язык обратного перевода и обновить живой перевод."""
        normalized_language = lang.upper()
        if normalized_language == "EN":
            normalized_language = "EN-US"

        if normalized_language not in SUPPORTED_TARGET_LANGUAGES:
            raise ValueError("target language must be 'RU', 'EN-US' or 'ZH'")

        self._target_lang = normalized_language
        self._config["reverse_target_lang"] = normalized_language
        self._update_language_buttons()
        self._schedule_translation(self._input_field.text())

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            self.hide()

        return super().event(event)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self._input_field and event.type() == QEvent.Type.KeyPress:
            key_event = event
            if isinstance(key_event, QKeyEvent):
                return self._handle_key_press(key_event)

        return super().eventFilter(watched, event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        QTimer.singleShot(0, self._hide_if_inactive)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._handle_key_press(event):
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None

        super().mouseReleaseEvent(event)

    @pyqtSlot(int, str)
    def _apply_translation(self, request_id: int, translation: str) -> None:
        if request_id != self._translation_request_id or not self.isVisible():
            return

        self._translated_text = translation
        self._translation_label.setText(translation)
        self._copy_translation_button.setEnabled(
            bool(translation) and translation != TRANSLATION_ERROR
        )

    def _configure_window(self) -> None:
        self.setObjectName("inputWindow")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(500, 130)

        self.setStyleSheet(
            """
            QWidget#inputWindow {
                background-color: rgba(20, 20, 20, 230);
                border-radius: 6px;
            }
            QLineEdit {
                background-color: rgba(45, 45, 45, 240);
                color: #FFFFFF;
                border: 1px solid #555555;
                border-radius: 4px;
                padding: 6px 8px;
                font-size: 14px;
                selection-background-color: #4FC3F7;
            }
            QLineEdit:focus {
                border-color: #4FC3F7;
            }
            QLabel#translationLabel {
                background-color: transparent;
                color: #4FC3F7;
                font-size: 13px;
            }
            QLabel#modeIndicator {
                background-color: rgba(79, 195, 247, 35);
                color: #4FC3F7;
                border: 1px solid #4FC3F7;
                border-radius: 4px;
                padding: 3px 9px;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton {
                background-color: #333333;
                color: #BBBBBB;
                border: 1px solid #555555;
                border-radius: 4px;
                padding: 3px 12px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                border-color: #4FC3F7;
                color: #FFFFFF;
            }
            QPushButton:disabled {
                background-color: #2A2A2A;
                border-color: #444444;
                color: #777777;
            }
            QPushButton:checked {
                background-color: #4FC3F7;
                border-color: #4FC3F7;
                color: #141414;
            }
            """
        )

    def _create_interface(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 10, 12, 10)
        root_layout.setSpacing(6)

        self._input_field = QLineEdit(self)
        self._input_field.setPlaceholderText("Введите сообщение...")
        self._input_field.installEventFilter(self)

        self._copy_button = QPushButton("Копировать текст", self)
        self._copy_button.setToolTip("Скопировать введённый текст")
        self._copy_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._copy_button.setEnabled(False)

        input_layout = QHBoxLayout()
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(6)
        input_layout.addWidget(self._input_field, 1)
        input_layout.addWidget(self._copy_button)

        self._translation_label = QLabel("", self)
        self._translation_label.setObjectName("translationLabel")
        self._translation_label.setTextFormat(Qt.TextFormat.PlainText)
        self._translation_label.setMinimumHeight(20)
        self._translation_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )

        self._copy_translation_button = QPushButton(
            "Копировать перевод", self
        )
        self._copy_translation_button.setToolTip(
            "Скопировать готовый перевод"
        )
        self._copy_translation_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._copy_translation_button.setEnabled(False)

        translation_layout = QHBoxLayout()
        translation_layout.setContentsMargins(0, 0, 0, 0)
        translation_layout.setSpacing(6)
        translation_layout.addWidget(self._translation_label, 1)
        translation_layout.addWidget(self._copy_translation_button)

        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(5)

        self._language_group = QButtonGroup(self)
        self._language_group.setExclusive(True)

        for caption, language_code in (
            ("RU", "RU"),
            ("EN", "EN-US"),
            ("ZH", "ZH"),
        ):
            button = QPushButton(caption, self)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(
                lambda _checked, lang=language_code: self.set_target_lang(lang)
            )
            self._language_group.addButton(button)
            self._language_buttons[language_code] = button
            controls_layout.addWidget(button)

        controls_layout.addStretch(1)

        self._mode_indicator = QLabel("", self)
        self._mode_indicator.setObjectName("modeIndicator")
        self._mode_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mode_indicator.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        controls_layout.addWidget(self._mode_indicator)

        root_layout.addLayout(input_layout)
        root_layout.addLayout(translation_layout)
        root_layout.addLayout(controls_layout)

    def _connect_signals(self) -> None:
        self._input_field.textChanged.connect(self._schedule_translation)
        self._input_field.textChanged.connect(
            lambda text: self._copy_button.setEnabled(bool(text))
        )
        self._copy_button.clicked.connect(self._copy_input_text)
        self._copy_translation_button.clicked.connect(
            self._copy_translated_text
        )
        self._translation_ready.connect(self._apply_translation)

    @pyqtSlot()
    def _copy_input_text(self) -> None:
        """Скопировать оригинал, не закрывая окно и не сбрасывая ввод."""
        QApplication.clipboard().setText(self._input_field.text())

    @pyqtSlot()
    def _copy_translated_text(self) -> None:
        """Скопировать готовый перевод без отправки сообщения в игру."""
        if self._translated_text and self._translated_text != TRANSLATION_ERROR:
            QApplication.clipboard().setText(self._translated_text)

    def _schedule_translation(self, text: str) -> None:
        self._translation_request_id += 1
        request_id = self._translation_request_id
        target_language = self._target_lang

        self._translated_text = ""
        self._translation_label.clear()
        self._copy_translation_button.setEnabled(False)

        if not text.strip():
            return

        self._translation_label.setText("Перевод...")
        QTimer.singleShot(
            DEBOUNCE_INTERVAL_MS,
            lambda: self._start_translation(
                request_id,
                text,
                target_language,
            ),
        )

    def _start_translation(
        self,
        request_id: int,
        text: str,
        target_language: str,
    ) -> None:
        if (
            request_id != self._translation_request_id
            or text != self._input_field.text()
            or target_language != self._target_lang
            or not self.isVisible()
        ):
            return

        self._translator.translate_async(
            text,
            target_language,
            lambda translation: self._translation_ready.emit(
                request_id,
                translation,
            ),
        )

    def _handle_key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return True

        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._submit_translation()
            event.accept()
            return True

        if event.key() == Qt.Key.Key_Tab:
            new_mode = "team" if self._chat_mode == "all" else "all"
            self.set_chat_mode(new_mode)
            event.accept()
            return True

        return False

    def _submit_translation(self) -> None:
        if (
            not self._translated_text
            or self._translated_text == TRANSLATION_ERROR
        ):
            return

        translated_text = self._translated_text
        chat_mode = self._chat_mode
        self.hide()
        QTimer.singleShot(
            120,
            lambda: self.send_message.emit(translated_text, chat_mode),
        )

    def _reset_input(self) -> None:
        self._translation_request_id += 1
        self._translated_text = ""
        self._input_field.clear()
        self._translation_label.clear()
        self._copy_translation_button.setEnabled(False)
        self._drag_offset = None

    def _hide_if_inactive(self) -> None:
        if self.isVisible() and not self.isActiveWindow():
            self.hide()

    def _update_language_buttons(self) -> None:
        for language_code, button in self._language_buttons.items():
            button.setChecked(language_code == self._target_lang)

    def _update_chat_mode_indicator(self) -> None:
        self._mode_indicator.setText(f"[{self._chat_mode.upper()}]")


if __name__ == "__main__":
    import threading
    import time

    class TranslatorMock:
        """Имитирует асинхронный перевод без обращения к DeepL."""

        def translate_async(
            self,
            text: str,
            target_lang: str,
            callback,
        ) -> None:
            def worker() -> None:
                time.sleep(0.15)
                callback(f"{target_lang}: {text}")

            threading.Thread(target=worker, daemon=True).start()

    demo_config = {
        "reverse_target_lang": "EN-US",
        "chat_mode": "all",
    }

    app = QApplication(sys.argv)
    window = InputWindow(TranslatorMock(), demo_config)
    window.send_message.connect(
        lambda text, mode: print(f"SEND [{mode}]: {text}")
    )
    window.show()
    sys.exit(app.exec())
