"""Загрузка и конвертация цен TF2 из backpack.tf API."""

import json
import math
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Iterator, Optional

from core.backpack_profile_parser import (
    BackpackInventoryValues,
    parse_backpack_profile_values,
)


PRICES_ENDPOINT = "https://backpack.tf/api/IGetPrices/v4"
INVENTORY_VALUES_ENDPOINT = (
    "https://api.backpack.tf/api/inventory/{steamid}/values"
)
PROFILE_ENDPOINT = "https://backpack.tf/profiles/{steamid}"
HTTP_TIMEOUT = 10.0
CACHE_TTL_SECONDS = 60 * 60
FAILURE_RETRY_SECONDS = 60.0
DEFAULT_KEY_PRICE_USD = 2.5
METAL_PER_KEY = 54.0  # fallback если raw_usd_value недоступен
KEY_ITEM_NAME = "Mann Co. Supply Crate Key"
UNIQUE_QUALITY = 6


class PriceFetcher:
    """Хранит часовую копию базы цен backpack.tf в памяти."""

    def __init__(self, api_key: str, access_token: str = "") -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._prices_db: dict = {}
        self._metal_price_usd: Optional[float] = None
        self._last_loaded_at: Optional[float] = None
        self._retry_after = 0.0
        self._cache_lock = threading.RLock()
        self._load_future: Optional[Future] = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tf2-price-fetcher",
        )
        self._closed = False
        self._profile_probe_lock = threading.Lock()
        self._profile_scraping_available: Optional[bool] = None

    def load_prices(self) -> bool:
        """Загрузить цены в executor-потоке и вернуть итог операции."""
        with self._cache_lock:
            if self._is_cache_fresh_locked():
                return True
            if self._closed:
                self._log_error("price loader is already shut down")
                return False
            if time.monotonic() < self._retry_after:
                return False

            future = self._load_future
            if future is None or future.done():
                try:
                    future = self._executor.submit(self._load_prices_worker)
                except RuntimeError as error:
                    self._retry_after = (
                        time.monotonic() + FAILURE_RETRY_SECONDS
                    )
                    self._log_error(f"failed to schedule price loading: {error}")
                    return False
                self._load_future = future

        try:
            return bool(future.result())
        except Exception as error:
            with self._cache_lock:
                self._retry_after = time.monotonic() + FAILURE_RETRY_SECONDS
            self._log_error(f"unexpected price loading failure: {error}")
            return False
        finally:
            with self._cache_lock:
                if self._load_future is future and future.done():
                    self._load_future = None

    def get_item_price_usd(
        self,
        item_name: str,
        quality: int,
    ) -> Optional[float]:
        """Вернуть первую craftable/tradable цену предмета в долларах."""
        price = self._find_first_price(item_name, quality)
        if price is None:
            return None

        value = self._read_numeric_value(price)
        if value is None:
            return None

        currency = str(price.get("currency", "")).strip().lower()
        if currency == "usd":
            return value

        key_price_usd = self.get_key_price_usd()
        if currency == "keys":
            return value * key_price_usd
        if currency == "metal":
            with self._cache_lock:
                metal_usd = self._metal_price_usd
            if metal_usd is not None:
                return value * metal_usd
            return value / METAL_PER_KEY * key_price_usd

        return None

    def get_key_price_usd(self) -> float:
        """Вернуть USD-цену ключа или безопасное значение 2.5 доллара."""
        prices = self._find_prices(KEY_ITEM_NAME, UNIQUE_QUALITY)
        with self._cache_lock:
            metal_usd = self._metal_price_usd

        # Прямая USD-цена предпочтительнее пересчёта через metal.
        for price in prices:
            currency = str(price.get("currency", "")).strip().lower()
            value = self._read_numeric_value(price)
            if value is not None and currency == "usd":
                return value

        for price in prices:
            currency = str(price.get("currency", "")).strip().lower()
            value = self._read_numeric_value(price)
            if value is not None and currency == "metal" and metal_usd is not None:
                return value * metal_usd
            if value is not None and currency == "keys":
                return value * DEFAULT_KEY_PRICE_USD

        return DEFAULT_KEY_PRICE_USD

    def get_inventory_values_usd(
        self,
        steamid64: str,
    ) -> Optional[BackpackInventoryValues]:
        """Получить готовую оценку backpack.tf для одного SteamID64."""
        normalized_steamid = str(steamid64).strip()
        if not normalized_steamid.isdigit():
            self._log_error(f"invalid SteamID64: {steamid64!r}")
            return None

        if self._access_token.strip():
            api_values = self._request_inventory_values_api(
                normalized_steamid
            )
            if api_values is not None:
                return api_values

        return self._load_profile_inventory_values(normalized_steamid)

    def shutdown(self) -> None:
        """Остановить executor; повторная загрузка после этого запрещена."""
        with self._cache_lock:
            if self._closed:
                return
            self._closed = True

        self._executor.shutdown(wait=True, cancel_futures=True)

    def _load_prices_worker(self) -> bool:
        if not self._api_key.strip():
            with self._cache_lock:
                self._retry_after = time.monotonic() + FAILURE_RETRY_SECONDS
            self._log_error("backpack.tf API key is empty")
            return False

        query = urllib.parse.urlencode({"key": self._api_key})
        api_url = f"{PRICES_ENDPOINT}?{query}"
        request = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "TF2ChatTranslatorOverlay/1.0",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

            # IGetPrices/v4 может отдавать данные напрямую или в {"response":{}}
            response_data = payload.get("response")
            if not isinstance(response_data, dict):
                response_data = payload
            if response_data.get("success") not in (1, True):
                raise ValueError("backpack.tf returned an unsuccessful response")

            items = response_data.get("items")
            if not isinstance(items, dict):
                raise ValueError("backpack.tf items database is missing")

            # raw_usd_value — цена одного refined metal в USD
            raw_usd_value = response_data.get("raw_usd_value")
            metal_price: Optional[float] = None
            if raw_usd_value is not None:
                try:
                    parsed_metal_price = float(raw_usd_value)
                    if (
                        math.isfinite(parsed_metal_price)
                        and parsed_metal_price > 0
                    ):
                        metal_price = parsed_metal_price
                except (TypeError, ValueError):
                    pass

            with self._cache_lock:
                self._prices_db = items
                self._metal_price_usd = metal_price
                self._last_loaded_at = time.monotonic()
                self._retry_after = 0.0
            return True
        except Exception as error:
            with self._cache_lock:
                self._retry_after = time.monotonic() + FAILURE_RETRY_SECONDS
            self._log_error(f"failed to load backpack.tf prices: {error}")
            return False

    def _load_profile_inventory_values(
        self,
        steamid64: str,
    ) -> Optional[BackpackInventoryValues]:
        with self._profile_probe_lock:
            if self._profile_scraping_available is False:
                return None
            is_initial_probe = self._profile_scraping_available is None
            values = self._request_profile_html(steamid64)
            if values is not None:
                self._profile_scraping_available = True
            elif is_initial_probe and self._profile_scraping_available is None:
                # Разметка могла отсутствовать у одного профиля; повтор разрешён.
                return None
            return values

    def _request_inventory_values_api(
        self,
        steamid64: str,
    ) -> Optional[BackpackInventoryValues]:
        request = urllib.request.Request(
            INVENTORY_VALUES_ENDPOINT.format(steamid=steamid64),
            headers={
                "Accept": "application/json",
                "User-Agent": "TF2ChatTranslatorOverlay/1.0",
                "X-Auth-Token": self._access_token.strip(),
                "X-App-Context": "440",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self._log_error(
                    "backpack.tf access token is invalid or expired"
                )
            elif error.code == 429:
                self._log_error("backpack.tf inventory API rate limit reached")
            else:
                self._log_error(
                    f"backpack.tf inventory API HTTP {error.code}"
                )
            return None
        except Exception as error:
            self._log_error(
                f"failed to load backpack.tf inventory values: {error}"
            )
            return None

        if not isinstance(payload, dict):
            self._log_error(
                "backpack.tf inventory API returned non-object JSON"
            )
            return None

        community_value_ref = self._parse_nonnegative_float(
            payload.get("value")
        )
        market_value = self._parse_nonnegative_float(
            payload.get("market_value")
        )
        community_value: Optional[float] = None
        if community_value_ref is not None:
            with self._cache_lock:
                metal_price_usd = self._metal_price_usd
            if metal_price_usd is None and self.load_prices():
                with self._cache_lock:
                    metal_price_usd = self._metal_price_usd
            if metal_price_usd is not None:
                # Inventory API отдаёт value в refined metal, а market_value в USD.
                community_value = community_value_ref * metal_price_usd
            else:
                self._log_error(
                    "cannot convert backpack.tf community value to USD"
                )
        if community_value is None and market_value is None:
            self._log_error(
                "backpack.tf inventory API response has no values"
            )
            return None

        return BackpackInventoryValues(
            community_value_usd=community_value,
            market_value_usd=market_value,
            source="inventory_api",
        )

    def _request_profile_html(
        self,
        steamid64: str,
    ) -> Optional[BackpackInventoryValues]:
        request = urllib.request.Request(
            PROFILE_ENDPOINT.format(steamid=steamid64),
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
                ),
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT,
            ) as response:
                final_url = response.geturl()
                html_text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code in {403, 429}:
                self._profile_scraping_available = False
                self._log_error(
                    "backpack.tf profile scraping is blocked; "
                    "using fallback valuation"
                )
            return None
        except Exception as error:
            self._log_error(f"failed to load backpack.tf profile: {error}")
            return None

        if "backpack.tf" not in urllib.parse.urlparse(final_url).netloc:
            self._profile_scraping_available = False
            self._log_error(
                "backpack.tf profile redirected away; "
                "using fallback valuation"
            )
            return None
        return parse_backpack_profile_values(html_text)

    def _find_first_price(
        self,
        item_name: str,
        quality: int,
    ) -> Optional[dict]:
        prices = self._find_prices(item_name, quality)
        return prices[0] if prices else None

    def _find_prices(self, item_name: str, quality: int) -> list[dict]:
        with self._cache_lock:
            prices_db = self._prices_db

        item = prices_db.get(item_name)
        if not isinstance(item, dict):
            return []

        all_prices = item.get("prices")
        if not isinstance(all_prices, dict):
            return []

        quality_prices = all_prices.get(str(quality))
        if quality_prices is None:
            quality_prices = all_prices.get(quality)
        if not isinstance(quality_prices, dict):
            return []

        tradable = quality_prices.get("Tradable")
        if not isinstance(tradable, dict):
            return []

        craftable = tradable.get("Craftable")
        return list(self._iter_price_records(craftable))

    @classmethod
    def _iter_price_records(cls, value: object) -> Iterator[dict]:
        if isinstance(value, list):
            for nested_value in value:
                yield from cls._iter_price_records(nested_value)
            return

        if not isinstance(value, dict):
            return

        if "currency" in value and "value" in value:
            yield value
            return

        # Unusual-цены могут быть словарём, индексированным по эффекту.
        for nested_value in value.values():
            yield from cls._iter_price_records(nested_value)

    def _is_cache_fresh_locked(self) -> bool:
        if self._last_loaded_at is None:
            return False
        return time.monotonic() - self._last_loaded_at < CACHE_TTL_SECONDS

    @staticmethod
    def _read_numeric_value(price: dict) -> Optional[float]:
        value = price.get("value")
        if isinstance(value, bool):
            return None

        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(numeric_value) or numeric_value < 0:
            return None
        return numeric_value

    @staticmethod
    def _parse_nonnegative_float(value: object) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            parsed_value = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed_value) or parsed_value < 0:
            return None
        return parsed_value

    @staticmethod
    def _log_error(message: str) -> None:
        print(f"PriceFetcher: {message}", file=sys.stderr)
