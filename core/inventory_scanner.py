"""Загрузка и оценка инвентарей игроков Team Fortress 2."""

import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from core.price_fetcher import PriceFetcher


INVENTORY_ENDPOINT = (
    "https://api.steampowered.com/IEconItems_440/GetPlayerItems/v1/"
)
# GetSchema/v2 → 404; актуальный endpoint с пагинацией
SCHEMA_ENDPOINT = (
    "https://api.steampowered.com/IEconItems_440/GetSchemaItems/v1/"
)
HTTP_TIMEOUT = 10.0
INVENTORY_CACHE_TTL_SECONDS = 5 * 60
ERROR_CACHE_TTL_SECONDS = 30
MIN_REQUEST_INTERVAL_SECONDS = 1.5  # Steam rate-limit: не чаще 1 rps
MAX_WORKERS = 5


@dataclass
class PlayerInventory:
    """Результат оценки инвентаря игрока."""

    steamid: str
    player_name: str
    team: str
    total_value_usd: Optional[float]
    item_count: int
    fetched_at: float
    # ready / private / api_error / prices_unavailable
    status: str = "ready"
    market_value_usd: Optional[float] = None


@dataclass(frozen=True)
class _InventoryCacheEntry:
    total_value_usd: Optional[float]
    item_count: int
    cached_at: float
    fetched_at: float
    status: str = "ready"
    market_value_usd: Optional[float] = None


class InventoryScanner:
    """Получает TF2-инвентари и оценивает их через ``PriceFetcher``."""

    def __init__(
        self,
        steam_api_key: str,
        price_fetcher: PriceFetcher,
    ) -> None:
        self._steam_api_key = steam_api_key
        self._price_fetcher = price_fetcher

        self._schema: dict[int, tuple[str, int]] = {}
        self._schema_loaded = False
        self._schema_lock = threading.Lock()

        self._inventory_cache: dict[str, _InventoryCacheEntry] = {}
        self._inventory_inflight: dict[str, threading.Event] = {}
        self._inventory_lock = threading.RLock()

        self._rate_limit_lock = threading.Lock()
        self._last_request_at: Optional[float] = None

        self._executor = ThreadPoolExecutor(
            max_workers=MAX_WORKERS,
            thread_name_prefix="tf2-inventory-scanner",
        )
        self._closed = False
        self._state_lock = threading.Lock()

    def load_schema(self) -> bool:
        """Загрузить defindex→(name, quality) постранично через GetSchemaItems/v1."""
        with self._schema_lock:
            if self._schema_loaded:
                return True

            schema: dict[int, tuple[str, int]] = {}
            start: Optional[str] = None
            seen_cursors: set[str] = set()

            while True:
                if self._closed:
                    return False

                parameters: dict[str, str] = {
                    "key": self._steam_api_key,
                    "language": "en",
                }
                if start is not None:
                    parameters["start"] = start

                payload = self._request_json(SCHEMA_ENDPOINT, parameters)
                if payload is None:
                    return False

                result = payload.get("result")
                if not isinstance(result, dict):
                    self._log_error("Steam schema result is missing")
                    return False
                if result.get("status", 1) != 1:
                    self._log_error(
                        f"Steam schema returned status {result.get('status')!r}"
                    )
                    return False

                items = result.get("items")
                if not isinstance(items, list):
                    self._log_error("Steam schema items list is missing")
                    return False

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        defindex = int(item["defindex"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    item_name = item.get("item_name") or item.get("name")
                    if not isinstance(item_name, str) or not item_name.strip():
                        continue
                    try:
                        quality = int(
                            item.get("item_quality", item.get("quality", 6))
                        )
                    except (TypeError, ValueError):
                        quality = 6
                    schema[defindex] = (item_name.strip(), quality)

                next_value = result.get("next")
                if next_value in (None, "", 0, "0"):
                    break
                next_cursor = str(next_value)
                if next_cursor in seen_cursors:
                    self._log_error("Steam schema pagination loop detected")
                    return False
                seen_cursors.add(next_cursor)
                start = next_cursor

            if not schema:
                self._log_error("Steam schema contains no usable TF2 items")
                return False

            self._schema = schema
            self._schema_loaded = True
            return True

    def get_inventory_value(self, steamid64: str) -> Optional[float]:
        """Вернуть стоимость публичного инвентаря с кешем на пять минут."""
        normalized_steamid = str(steamid64).strip()
        if not normalized_steamid or not normalized_steamid.isdigit():
            self._log_error(f"invalid SteamID64: {steamid64!r}")
            return None

        entry = self._get_or_fetch_inventory(normalized_steamid)
        return entry.total_value_usd

    def scan_players(
        self,
        players: list[dict],
        on_result: Callable[[PlayerInventory], None],
    ) -> list[Future]:
        """Асинхронно оценить игроков, у которых нет свежего кеша."""
        with self._state_lock:
            if self._closed:
                self._log_error("inventory scanner is already shut down")
                return []

        futures: list[Future] = []

        for player in players:
            if not isinstance(player, dict):
                self._log_error("player entry must be a dictionary")
                continue

            steamid = str(player.get("steamid", "")).strip()
            if not steamid or not steamid.isdigit():
                self._log_error(f"invalid player SteamID64: {steamid!r}")
                continue

            player_name = str(
                player.get("name", player.get("player_name", ""))
            )
            team = self._normalize_team(player.get("team", ""))

            cached = self._get_cached_inventory(steamid)
            if cached is not None:
                # Кеш свежий — сразу отдаём результат без HTTP-запроса
                result = PlayerInventory(
                    steamid=steamid,
                    player_name=player_name,
                    team=team,
                    total_value_usd=cached.total_value_usd,
                    item_count=cached.item_count,
                    fetched_at=cached.fetched_at,
                    status=cached.status,
                    market_value_usd=cached.market_value_usd,
                )
                try:
                    on_result(result)
                except Exception as error:
                    self._log_error(
                        f"inventory callback failed for cached entry: {error}"
                    )
                continue

            try:
                future = self._executor.submit(
                    self._scan_player,
                    steamid,
                    player_name,
                    team,
                    on_result,
                )
                futures.append(future)
            except RuntimeError as error:
                self._log_error(f"failed to schedule inventory scan: {error}")

        return futures

    def shutdown(self) -> None:
        """Дождаться активных сканирований и остановить executor."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True

        self._executor.shutdown(wait=True, cancel_futures=True)

    def _scan_player(
        self,
        steamid: str,
        player_name: str,
        team: str,
        on_result: Callable[[PlayerInventory], None],
    ) -> None:
        try:
            entry = self._get_or_fetch_inventory(steamid)
            result = PlayerInventory(
                steamid=steamid,
                player_name=player_name,
                team=team,
                total_value_usd=entry.total_value_usd,
                item_count=entry.item_count,
                fetched_at=entry.fetched_at,
                status=entry.status,
                market_value_usd=entry.market_value_usd,
            )
            try:
                on_result(result)
            except Exception as error:
                self._log_error(f"inventory callback failed: {error}")
        except Exception as error:
            self._log_error(f"inventory scan failed for {steamid}: {error}")

    def _get_or_fetch_inventory(self, steamid64: str) -> _InventoryCacheEntry:
        cached_entry = self._get_cached_inventory(steamid64)
        if cached_entry is not None:
            return cached_entry

        with self._inventory_lock:
            cached_entry = self._get_cached_inventory_locked(steamid64)
            if cached_entry is not None:
                return cached_entry

            wait_event = self._inventory_inflight.get(steamid64)
            owns_request = wait_event is None
            if wait_event is None:
                wait_event = threading.Event()
                self._inventory_inflight[steamid64] = wait_event

        if not owns_request:
            wait_event.wait(timeout=30.0)
            cached_entry = self._get_cached_inventory(steamid64)
            if cached_entry is not None:
                return cached_entry
            return self._make_unavailable_entry("api_error")

        try:
            entry = self._fetch_inventory(steamid64)
            with self._inventory_lock:
                self._inventory_cache[steamid64] = entry
            return entry
        except Exception as error:
            self._log_error(
                f"unexpected inventory failure for {steamid64}: {error}"
            )
            entry = self._make_unavailable_entry("api_error")
            with self._inventory_lock:
                self._inventory_cache[steamid64] = entry
            return entry
        finally:
            with self._inventory_lock:
                completed_event = self._inventory_inflight.pop(
                    steamid64,
                    None,
                )
                if completed_event is not None:
                    completed_event.set()

    def _fetch_inventory(self, steamid64: str) -> _InventoryCacheEntry:
        try:
            backpack_values = self._price_fetcher.get_inventory_values_usd(
                steamid64
            )
        except Exception as error:
            self._log_error(
                f"backpack.tf value lookup failed for {steamid64}: {error}"
            )
            backpack_values = None

        if (
            backpack_values is not None
            and backpack_values.community_value_usd is not None
        ):
            return _InventoryCacheEntry(
                total_value_usd=backpack_values.community_value_usd,
                market_value_usd=backpack_values.market_value_usd,
                item_count=0,
                cached_at=time.monotonic(),
                fetched_at=time.time(),
            )

        if not self.load_schema():
            return self._make_unavailable_entry("api_error")

        payload = self._request_json(
            INVENTORY_ENDPOINT,
            {
                "key": self._steam_api_key,
                "steamid": steamid64,
            },
        )
        if payload is None:
            return self._make_unavailable_entry("api_error")

        result = payload.get("result")
        if not isinstance(result, dict):
            self._log_error(f"Steam inventory result is missing for {steamid64}")
            return self._make_unavailable_entry("api_error")

        status_code = result.get("status")
        if status_code != 1:
            # status=15 — приватный инвентарь; иначе — ошибка API
            inv_status = "private" if status_code == 15 else "api_error"
            return self._make_unavailable_entry(inv_status)

        items = result.get("items")
        if not isinstance(items, list):
            self._log_error(f"Steam inventory items are missing for {steamid64}")
            return self._make_unavailable_entry("api_error")

        if items:
            try:
                prices_loaded = self._price_fetcher.load_prices()
            except Exception as error:
                self._log_error(f"price database loading failed: {error}")
                return self._make_unavailable_entry("api_error")
            if not prices_loaded:
                self._log_error("price database is unavailable")
                return self._make_unavailable_entry("prices_unavailable")

        total_value_usd = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue

            try:
                defindex = int(item["defindex"])
            except (KeyError, TypeError, ValueError):
                continue

            schema_item = self._schema.get(defindex)
            if schema_item is None:
                continue

            item_name, schema_quality = schema_item
            try:
                quality = int(item.get("quality", schema_quality))
            except (TypeError, ValueError):
                quality = schema_quality

            try:
                price = self._price_fetcher.get_item_price_usd(
                    item_name,
                    quality,
                )
            except Exception as error:
                self._log_error(
                    f"price lookup failed for {item_name!r}: {error}"
                )
                continue

            if price is not None:
                total_value_usd += price

        now = time.time()
        return _InventoryCacheEntry(
            total_value_usd=total_value_usd,
            item_count=len(items),
            cached_at=time.monotonic(),
            fetched_at=now,
            market_value_usd=None,
        )

    def _request_json(
        self,
        endpoint: str,
        parameters: dict[str, str],
    ) -> Optional[dict]:
        if not self._steam_api_key.strip():
            self._log_error("Steam API key is empty")
            return None

        query = urllib.parse.urlencode(parameters)
        request = urllib.request.Request(
            f"{endpoint}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "TF2ChatTranslatorOverlay/1.0",
            },
        )

        self._wait_for_rate_limit()
        try:
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 403:
                self._log_error(
                    "Steam API denied the request (403) — "
                    "check the API key and IP allowlist"
                )
            else:
                self._log_error(f"Steam API HTTP {error.code}")
            return None
        except Exception as error:
            self._log_error(f"Steam API request failed: {error}")
            return None

        if not isinstance(payload, dict):
            self._log_error("Steam API returned a non-object JSON response")
            return None
        return payload

    def _wait_for_rate_limit(self) -> None:
        with self._rate_limit_lock:
            now = time.monotonic()
            if self._last_request_at is not None:
                wait_seconds = (
                    self._last_request_at
                    + MIN_REQUEST_INTERVAL_SECONDS
                    - now
                )
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
            self._last_request_at = time.monotonic()

    def _get_cached_inventory(
        self,
        steamid64: str,
    ) -> Optional[_InventoryCacheEntry]:
        with self._inventory_lock:
            return self._get_cached_inventory_locked(steamid64)

    def _get_cached_inventory_locked(
        self,
        steamid64: str,
    ) -> Optional[_InventoryCacheEntry]:
        entry = self._inventory_cache.get(steamid64)
        if entry is None:
            return None

        cache_ttl = (
            INVENTORY_CACHE_TTL_SECONDS
            if entry.status in {"ready", "private"}
            else ERROR_CACHE_TTL_SECONDS
        )
        if time.monotonic() - entry.cached_at < cache_ttl:
            return entry

        del self._inventory_cache[steamid64]
        return None

    @staticmethod
    def _make_unavailable_entry(status: str = "api_error") -> _InventoryCacheEntry:
        return _InventoryCacheEntry(
            total_value_usd=None,
            item_count=0,
            cached_at=time.monotonic(),
            fetched_at=time.time(),
            status=status,
            market_value_usd=None,
        )

    @staticmethod
    def _normalize_team(team: object) -> str:
        normalized_team = str(team).strip().casefold()
        if normalized_team == "red":
            return "Red"
        if normalized_team == "blue":
            return "Blue"
        return ""

    @staticmethod
    def _log_error(message: str) -> None:
        print(f"InventoryScanner: {message}", file=sys.stderr)
