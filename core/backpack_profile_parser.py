"""Парсинг итоговой стоимости инвентаря со страницы backpack.tf."""

import math
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional


COMMUNITY_VALUE_CLASS = "community-value"
MARKET_VALUE_CLASS = "market-value"


@dataclass(frozen=True)
class BackpackInventoryValues:
    """Оценки инвентаря в долларах США."""

    community_value_usd: Optional[float]
    market_value_usd: Optional[float]
    source: str


class _ValueSpanParser(HTMLParser):
    """Собирает текст из нужных ``span``, включая вложенные элементы."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self._capturing: Optional[str] = None
        self._capture_depth = 0
        self._text_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
    ) -> None:
        if self._capturing is not None:
            self._capture_depth += 1
            return

        if tag.casefold() != "span":
            return

        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if COMMUNITY_VALUE_CLASS in classes:
            self._start_capture(COMMUNITY_VALUE_CLASS)
        elif MARKET_VALUE_CLASS in classes:
            self._start_capture(MARKET_VALUE_CLASS)

    def handle_endtag(self, _tag: str) -> None:
        if self._capturing is None:
            return

        self._capture_depth -= 1
        if self._capture_depth > 0:
            return

        self.values.setdefault(
            self._capturing,
            "".join(self._text_parts).strip(),
        )
        self._capturing = None
        self._text_parts.clear()

    def handle_data(self, data: str) -> None:
        if self._capturing is not None:
            self._text_parts.append(data)

    def _start_capture(self, value_class: str) -> None:
        self._capturing = value_class
        self._capture_depth = 1
        self._text_parts.clear()


def parse_backpack_profile_values(
    html_text: str,
) -> Optional[BackpackInventoryValues]:
    """Извлечь community/market стоимость из HTML профиля backpack.tf."""
    parser = _ValueSpanParser()
    try:
        parser.feed(html_text)
        parser.close()
    except (TypeError, ValueError):
        return None

    community_value = _parse_formatted_number(
        parser.values.get(COMMUNITY_VALUE_CLASS, "")
    )
    market_value = _parse_formatted_number(
        parser.values.get(MARKET_VALUE_CLASS, "")
    )
    if community_value is None and market_value is None:
        return None

    return BackpackInventoryValues(
        community_value_usd=community_value,
        market_value_usd=market_value,
        source="profile_html",
    )


def _parse_formatted_number(text: str) -> Optional[float]:
    normalized = re.sub(r"[^0-9.,+-]", "", text)
    if not normalized:
        return None

    if "," in normalized and "." in normalized:
        if normalized.rfind(".") > normalized.rfind(","):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized:
        comma_groups = normalized.split(",")
        if len(comma_groups) > 1 and all(
            len(group) == 3 for group in comma_groups[1:]
        ):
            normalized = "".join(comma_groups)
        else:
            normalized = normalized.replace(",", ".")

    try:
        value = float(normalized)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value
