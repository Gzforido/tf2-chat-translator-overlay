"""Перевод сообщений TF2 через DeepL с определением языка и LRU-кешем."""

import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import deepl
from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException


DetectorFactory.seed = 0

DEFAULT_LANGUAGE = "EN-US"
TRANSLATION_ERROR = "[Translation error]"
MAX_CACHE_SIZE = 500

LANGUAGE_CODES = {
    "ru": "RU",
    "en": "EN-US",
    "zh-cn": "ZH",
    "zh-tw": "ZH",
    "de": "DE",
    "fr": "FR",
    "es": "ES",
    "it": "IT",
    "pl": "PL",
    "pt": "PT-PT",
    "nl": "NL",
    "ja": "JA",
    "ko": "KO",
    "tr": "TR",
    "uk": "UK",
}


class Translator:
    """Потокобезопасная обёртка над DeepL для коротких сообщений чата."""

    def __init__(self, api_key: str) -> None:
        self._translator = deepl.Translator(api_key)
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=3,
            thread_name_prefix="tf2-translator",
        )

    def _detect_supported_language(self, text: str) -> Optional[str]:
        """Вернуть код поддерживаемого языка или ``None``."""
        try:
            detected_language = detect(text).lower()
        except LangDetectException as error:
            self._log_error("Language detection failed", error)
            return None

        return LANGUAGE_CODES.get(detected_language)

    def detect_language(self, text: str) -> str:
        """Публично вернуть определённый язык или безопасный дефолт."""
        return self._detect_supported_language(text) or DEFAULT_LANGUAGE

    def translate(self, text: str, target_lang: str) -> str:
        """Синхронно перевести текст или вернуть безопасный результат ошибки."""
        if not text or not any(character.isalpha() for character in text):
            return text

        normalized_target_lang = target_lang.upper()
        source_lang = self._detect_supported_language(text)
        if source_lang is not None and source_lang == normalized_target_lang:
            return text

        cache_key = (text, normalized_target_lang)
        cached_translation = self._get_cached(cache_key)
        if cached_translation is not None:
            return cached_translation

        try:
            result = self._translator.translate_text(
                text,
                target_lang=normalized_target_lang,
            )
        except deepl.QuotaExceededException as error:
            self._log_error("DeepL quota exceeded", error)
            return TRANSLATION_ERROR
        except deepl.DeepLException as error:
            self._log_error("DeepL request failed", error)
            return TRANSLATION_ERROR

        translated_text = result.text
        self._store_in_cache(cache_key, translated_text)
        return translated_text

    def translate_async(
        self,
        text: str,
        target_lang: str,
        callback: Callable[[str], None],
    ) -> None:
        """Запланировать перевод и callback в одном executor-потоке."""

        def worker() -> None:
            try:
                result = self.translate(text, target_lang)
            except Exception as error:
                self._log_error("Unexpected translation failure", error)
                result = TRANSLATION_ERROR

            try:
                callback(result)
            except Exception as error:
                # Ошибка UI-callback не должна завершать остальные переводы.
                self._log_error("Translation callback failed", error)

        try:
            self._executor.submit(worker)
        except RuntimeError as error:
            self._log_error(
                "Failed to schedule translation (executor shut down)",
                error,
            )
            try:
                callback(TRANSLATION_ERROR)
            except Exception as callback_error:
                self._log_error(
                    "Translation callback failed",
                    callback_error,
                )

    def shutdown(self) -> None:
        """Завершить executor и освободить ресурсы клиента DeepL."""
        self._executor.shutdown(wait=True, cancel_futures=True)
        close = getattr(self._translator, "close", None)
        if callable(close):
            close()

    def _get_cached(self, key: tuple[str, str]) -> Optional[str]:
        with self._cache_lock:
            try:
                cached_translation = self._cache.pop(key)
            except KeyError:
                return None

            # Повторная вставка перемещает запись в конец LRU-кеша.
            self._cache[key] = cached_translation
            return cached_translation

    def _store_in_cache(self, key: tuple[str, str], value: str) -> None:
        with self._cache_lock:
            self._cache.pop(key, None)
            self._cache[key] = value

            if len(self._cache) > MAX_CACHE_SIZE:
                self._cache.popitem(last=False)

    @staticmethod
    def _log_error(message: str, error: Exception) -> None:
        print(f"{message}: {error}", file=sys.stderr)
