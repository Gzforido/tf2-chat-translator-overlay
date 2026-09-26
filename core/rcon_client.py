"""Минимальный потокобезопасный клиент Source RCON для Team Fortress 2."""

import re
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from utils.tf2_detect import find_tf2_rcon_host


SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH = 3

CONNECT_TIMEOUT = 2.0
RESPONSE_TIMEOUT = 3.0
MIN_PACKET_SIZE = 10
MAX_PACKET_SIZE = 4096
MAX_RESPONSE_SIZE = 65_536
MAX_AUTH_PACKETS = 5
MAX_REQUEST_ID = 2_147_483_647
STATUS_WAIT_SECONDS = 4.0
STATUS_QUIET_SECONDS = 0.5
MAX_STATUS_BYTES = 256_000
STATUS_CACHE_SECONDS = 15.0
LOBBY_WAIT_SECONDS = 2.0
LOBBY_QUIET_SECONDS = 0.2
MAX_LOBBY_BYTES = 64_000
MAX_SESSION_LOBBY_BYTES = 4_000_000

STEAMID64_BASE = 76_561_197_960_265_728

_STATUS_PLAYER_PATTERN = re.compile(
    r'^\s*#\s+(?P<userid>\d+)\s+"'
    r'(?P<name>(?:\\.|[^"\\])*)"\s+'
    r'\[U:1:(?P<account_id>\d+)\](?P<details>.*)$'
)
_STATUS_HEADER_PATTERN = re.compile(r"^\s*version\s*:", re.IGNORECASE)
_STATUS_HUMAN_COUNT_PATTERN = re.compile(
    r"^\s*players\s*:\s*(?P<count>\d+)\s+humans\b",
    re.IGNORECASE,
)
_TEAM_PATTERN = re.compile(r"\b(red|blue)\b", re.IGNORECASE)
_LOBBY_HEADER_PATTERN = re.compile(
    r"CTFLobbyShared:.*?\b(?P<count>\d+)\s+member\(s\)"
)
_LOBBY_MEMBER_PATTERN = re.compile(
    r"Member\[\d+\]\s+\[U:1:(?P<account_id>\d+)\]"
    r"\s+team\s*=\s*(?P<team>\w+)"
)
_LOBBY_TEAMS = {
    "TF_GC_TEAM_DEFENDERS": "Defenders",
    "TF_GC_TEAM_INVADERS": "Invaders",
}
_SESSION_BOUNDARY_PATTERN = re.compile(
    r"^(?:Connected to |Disconnect:|Disconnecting from|Disconnected from)",
    re.MULTILINE,
)
_ESCAPED_NAME_CHARACTER = re.compile(r'\\(["\\])')


class _RconProtocolError(Exception):
    """Ответ сервера не соответствует формату Source RCON."""


class _RconAuthenticationError(Exception):
    """Сервер отклонил RCON-пароль."""


class RconClient:
    """Подключается к TF2 по Source RCON и выполняет команды."""

    def __init__(
        self,
        host: str = "auto",
        port: int = 27015,
        password: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._socket: Optional[socket.socket] = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._cached_players: list[dict] = []
        self._cached_players_at = 0.0
        self._cache_generation = 0

    def connect(self) -> bool:
        """Подключиться к серверу и авторизоваться с таймаутом 2 секунды."""
        with self._lock:
            return self._connect_locked()

    def disconnect(self) -> None:
        """Закрыть активное RCON-соединение."""
        with self._lock:
            self._disconnect_locked(log_errors=True)

    def clear_player_cache(self) -> None:
        """Не использовать состав прошлого матча после отключения."""
        with self._lock:
            self._clear_player_cache_locked()

    def execute(self, command: str) -> Optional[str]:
        """Выполнить команду или вернуть ``None`` при сетевой ошибке."""
        with self._lock:
            if self._socket is None and not self._connect_locked():
                return None

            try:
                return self._execute_locked(command)
            except Exception as error:
                self._log_error("command execution", error)
                self._disconnect_locked(log_errors=False)
                return None

    def get_players(self, log_path: Path) -> Optional[list[dict]]:
        """Читать свежий ``status`` с кратким резервом из текущего матча."""
        with self._lock:
            cache_generation = self._cache_generation
        try:
            initial_stat = log_path.stat()
        except OSError as error:
            self._log_error("status log read", error)
            return None

        position = initial_stat.st_size
        file_identity = (initial_stat.st_dev, initial_stat.st_ino)
        response = self.execute("status")
        if response is None:
            return None

        # TF2 -usercon пишет status в console.log, но иногда пропускает
        # вывод команды. Здесь учитываем только байты после запроса.
        deadline = time.monotonic() + STATUS_WAIT_SECONDS
        last_append: Optional[float] = None
        new_output = bytearray()
        while True:
            try:
                current_stat = log_path.stat()
                current_identity = (
                    current_stat.st_dev,
                    current_stat.st_ino,
                )
                if current_identity != file_identity or current_stat.st_size < position:
                    file_identity = current_identity
                    position = 0
                    new_output.clear()
                if current_stat.st_size > position:
                    with log_path.open("rb") as log_file:
                        log_file.seek(position)
                        chunk = log_file.read(MAX_STATUS_BYTES + 1)
                        position = log_file.tell()
                    new_output.extend(chunk)
                    last_append = time.monotonic()
                    if len(new_output) > MAX_STATUS_BYTES:
                        self._log_error(
                            "status log read",
                            ValueError("status output exceeded size limit"),
                        )
                        return None
            except OSError as error:
                self._log_error("status log read", error)
                return None

            lines = new_output.decode("utf-8", errors="replace").splitlines()
            has_header = any(_STATUS_HEADER_PATTERN.match(line) for line in lines)
            expected_counts = [
                int(match.group("count"))
                for line in lines
                if (match := _STATUS_HUMAN_COUNT_PATTERN.match(line))
            ]
            expected_count = expected_counts[-1] if expected_counts else None
            player_ids = {
                match.group("account_id")
                for line in lines
                if (match := _STATUS_PLAYER_PATTERN.match(line))
            }
            now = time.monotonic()
            if (
                has_header
                and expected_count is not None
                and len(player_ids) >= expected_count
                and last_append is not None
                and now - last_append >= STATUS_QUIET_SECONDS
            ):
                break
            if now >= deadline:
                break
            time.sleep(0.1)

        if not has_header:
            print(
                f"RCON status output missing: captured {len(new_output)} bytes",
                file=sys.stderr,
            )

        # При параллельном выводе консоли строки status могут появиться
        # до последнего заголовка version. Берём все новые строки без дублей.
        players_by_steamid: dict[str, dict] = {}
        for line in lines:
            match = _STATUS_PLAYER_PATTERN.match(line)
            if match is None:
                continue

            account_id = int(match.group("account_id"))
            steamid64 = account_id + STEAMID64_BASE
            player_name = _ESCAPED_NAME_CHARACTER.sub(
                r"\1",
                match.group("name"),
            )

            team_match = _TEAM_PATTERN.search(match.group("details"))
            team = team_match.group(1).capitalize() if team_match else ""

            players_by_steamid[str(steamid64)] = {
                "name": player_name,
                "steamid": str(steamid64),
                "team": team,
                "userid": match.group("userid"),
            }

        players = list(players_by_steamid.values())
        if has_header and expected_count == 0:
            self.clear_player_cache()
            return []

        status_complete = (
            has_header
            and expected_count is not None
            and bool(players)
            and len(players) >= expected_count
        )
        if not status_complete:
            print(
                "RCON status output incomplete"
                f" (found {len(players)}, expected {expected_count})",
                file=sys.stderr,
            )
            with self._lock:
                if (
                    cache_generation == self._cache_generation
                    and time.monotonic() - self._cached_players_at
                    <= STATUS_CACHE_SECONDS
                ):
                    players = [dict(player) for player in self._cached_players]
                else:
                    players = []

        # На многих серверах status не содержит команд. Матчмейкинговое
        # лобби, если оно доступно, связывает SteamID3 с командой.
        if not players:
            return None

        try:
            lobby_stat = log_path.stat()
        except OSError as error:
            self._log_error("lobby log read", error)
            lobby_stat = None

        lobby_response = self.execute("tf_lobby_debug") or ""
        if not _LOBBY_MEMBER_PATTERN.search(lobby_response) and lobby_stat is not None:
            # -usercon иногда пишет вывод команды только в console.log.
            # Читаем исключительно байты, появившиеся после запроса.
            position = lobby_stat.st_size
            file_identity = (lobby_stat.st_dev, lobby_stat.st_ino)
            deadline = time.monotonic() + LOBBY_WAIT_SECONDS
            last_append: Optional[float] = None
            lobby_log = bytearray()
            while time.monotonic() < deadline:
                try:
                    current_stat = log_path.stat()
                    current_identity = (current_stat.st_dev, current_stat.st_ino)
                    if current_identity != file_identity or current_stat.st_size < position:
                        file_identity = current_identity
                        position = 0
                        lobby_log.clear()
                    if current_stat.st_size > position:
                        with log_path.open("rb") as log_file:
                            log_file.seek(position)
                            chunk = log_file.read(MAX_LOBBY_BYTES + 1)
                            position = log_file.tell()
                        lobby_log.extend(chunk)
                        last_append = time.monotonic()
                        if len(lobby_log) > MAX_LOBBY_BYTES:
                            self._log_error(
                                "lobby log read",
                                ValueError("lobby output exceeded size limit"),
                            )
                            break
                except OSError as error:
                    self._log_error("lobby log read", error)
                    break

                if (
                    last_append is not None
                    and _LOBBY_MEMBER_PATTERN.search(
                        lobby_log.decode("utf-8", errors="replace")
                    )
                    and time.monotonic() - last_append >= LOBBY_QUIET_SECONDS
                ):
                    break
                time.sleep(0.05)

            lobby_response += "\n" + lobby_log.decode("utf-8", errors="replace")

        teams_by_steamid = self._parse_lobby_teams(lobby_response)
        if not teams_by_steamid:
            # Команда, введённая вручную в консоли, уже могла напечатать
            # снимок лобби до нашего RCON-запроса. Не читать прошлый матч.
            teams_by_steamid = self._read_session_lobby_teams(log_path)
        for player in players:
            player["team"] = (
                teams_by_steamid.get(player["steamid"]) or player["team"]
            )

        with self._lock:
            cached_teams = (
                {
                    player["steamid"]: player["team"]
                    for player in self._cached_players
                }
                if (
                    cache_generation == self._cache_generation
                    and time.monotonic() - self._cached_players_at
                    <= STATUS_CACHE_SECONDS
                )
                else {}
            )
        for player in players:
            if not player["team"]:
                player["team"] = cached_teams.get(player["steamid"], "")

        if status_complete:
            with self._lock:
                if cache_generation == self._cache_generation:
                    self._cached_players = [dict(player) for player in players]
                    self._cached_players_at = time.monotonic()

        return players

    @staticmethod
    def _parse_lobby_teams(output: str) -> dict[str, str]:
        header_matches = list(_LOBBY_HEADER_PATTERN.finditer(output))
        if not header_matches:
            return {}
        header = header_matches[-1]
        snapshot = output[header.end():]
        teams = {
            str(STEAMID64_BASE + int(match.group("account_id"))): (
                _LOBBY_TEAMS.get(match.group("team"), "")
            )
            for match in _LOBBY_MEMBER_PATTERN.finditer(snapshot)
        }
        if len(teams) < int(header.group("count")):
            return {}
        return teams

    def _read_session_lobby_teams(self, log_path: Path) -> dict[str, str]:
        """Последний полный снимок лобби только после текущего подключения."""
        try:
            with log_path.open("rb") as log_file:
                log_file.seek(0, 2)
                size = log_file.tell()
                log_file.seek(max(0, size - MAX_SESSION_LOBBY_BYTES))
                output = log_file.read().decode("utf-8", errors="replace")
        except OSError as error:
            self._log_error("lobby log read", error)
            return {}

        boundaries = list(_SESSION_BOUNDARY_PATTERN.finditer(output))
        if not boundaries:
            return {}
        latest_boundary = boundaries[-1]
        if not latest_boundary.group().startswith("Connected to "):
            return {}
        return self._parse_lobby_teams(output[latest_boundary.end():])

    def _connect_locked(self) -> bool:
        if self._socket is not None:
            return True

        host = (
            find_tf2_rcon_host(self._port)
            if self._host == "auto"
            else self._host
        )

        print(f"RCON connecting to {host}:{self._port}", file=sys.stderr)
        try:
            connection = socket.create_connection(
                (host, self._port),
                timeout=CONNECT_TIMEOUT,
            )
            print("RCON TCP connected, sending auth...", file=sys.stderr)
            connection.settimeout(RESPONSE_TIMEOUT)
            self._socket = connection

            request_id = self._next_request_id_locked()
            self._send_packet_locked(
                request_id,
                SERVERDATA_AUTH,
                self._password,
            )

            for _ in range(MAX_AUTH_PACKETS):
                response_id, response_type, _, _ = (
                    self._receive_packet_locked()
                )
                if response_type == SERVERDATA_AUTH_RESPONSE:
                    if response_id == -1:
                        raise _RconAuthenticationError(
                            "server rejected the RCON password"
                        )
                    if response_id != request_id:
                        raise _RconProtocolError(
                            "authentication response ID does not match request"
                        )
                    return True

                # TF2 -usercon quirk: responds with type=0 instead of type=2.
                # Try to read a follow-up type=2 within a short window; if none
                # arrives, treat this type=0 as successful authentication.
                if (
                    response_type == SERVERDATA_RESPONSE_VALUE
                    and response_id == request_id
                ):
                    self._socket.settimeout(0.5)
                    try:
                        r2_id, r2_type, _, _ = self._receive_packet_locked()
                        if r2_type == SERVERDATA_AUTH_RESPONSE:
                            if r2_id == -1:
                                raise _RconAuthenticationError(
                                    "server rejected the RCON password"
                                )
                            return True
                    except OSError:
                        pass
                    finally:
                        self._socket.settimeout(RESPONSE_TIMEOUT)
                    return True

            raise _RconProtocolError("authentication response was not received")
        except Exception as error:
            self._log_error("connection", error)
            self._disconnect_locked(log_errors=False)
            return False

    def _execute_locked(self, command: str) -> str:
        request_id = self._next_request_id_locked()
        terminator_id = self._next_request_id_locked()
        self._send_packet_locked(
            request_id,
            SERVERDATA_EXECCOMMAND,
            command,
        )
        self._send_packet_locked(
            terminator_id,
            SERVERDATA_EXECCOMMAND,
            "",
        )

        response_parts: list[str] = []
        total_size = 0
        while True:
            response_id, response_type, body, packet_size = (
                self._receive_packet_locked()
            )
            if response_id == terminator_id:
                break

            self._validate_command_response(
                response_id,
                response_type,
                request_id,
            )
            total_size += packet_size
            if total_size > MAX_RESPONSE_SIZE:
                raise _RconProtocolError("RCON response is too large")
            response_parts.append(body)

        return "".join(response_parts)

    def _send_packet_locked(
        self,
        request_id: int,
        packet_type: int,
        body: str,
    ) -> None:
        if self._socket is None:
            raise ConnectionError("RCON socket is not connected")

        encoded_body = body.encode("utf-8")
        if b"\x00" in encoded_body:
            raise ValueError("RCON packet body must not contain NUL bytes")

        packet_size = 4 + 4 + len(encoded_body) + 2
        if packet_size > MAX_PACKET_SIZE:
            raise ValueError("RCON packet exceeds the maximum packet size")

        packet = (
            struct.pack("<iii", packet_size, request_id, packet_type)
            + encoded_body
            + b"\x00\x00"
        )
        self._socket.sendall(packet)

    def _receive_packet_locked(self) -> tuple[int, int, str, int]:
        size_data = self._receive_exact_locked(4)
        (packet_size,) = struct.unpack("<i", size_data)
        if not MIN_PACKET_SIZE <= packet_size <= MAX_PACKET_SIZE:
            raise _RconProtocolError(
                f"invalid RCON packet size: {packet_size}"
            )

        packet_data = self._receive_exact_locked(packet_size)
        response_id, response_type = struct.unpack("<ii", packet_data[:8])
        terminated_body = packet_data[8:]
        if not terminated_body.endswith(b"\x00\x00"):
            raise _RconProtocolError("RCON packet has invalid terminators")

        body = terminated_body[:-2].decode("utf-8", errors="replace")
        return response_id, response_type, body, packet_size

    def _receive_exact_locked(self, byte_count: int) -> bytes:
        if self._socket is None:
            raise ConnectionError("RCON socket is not connected")

        received = bytearray()
        while len(received) < byte_count:
            chunk = self._socket.recv(byte_count - len(received))
            if not chunk:
                raise ConnectionError("RCON server closed the connection")
            received.extend(chunk)

        return bytes(received)

    def _next_request_id_locked(self) -> int:
        self._request_id += 1
        if self._request_id > MAX_REQUEST_ID:
            self._request_id = 1
        return self._request_id

    def _disconnect_locked(self, log_errors: bool) -> None:
        connection = self._socket
        self._socket = None
        self._clear_player_cache_locked()
        if connection is None:
            return

        try:
            connection.close()
        except OSError as error:
            if log_errors:
                self._log_error("disconnect", error)

    def _clear_player_cache_locked(self) -> None:
        self._cached_players.clear()
        self._cached_players_at = 0.0
        self._cache_generation += 1

    @staticmethod
    def _validate_command_response(
        response_id: int,
        response_type: int,
        request_id: int,
    ) -> None:
        if response_id != request_id:
            raise _RconProtocolError(
                f"response ID {response_id} does not match request {request_id}"
            )
        if response_type != SERVERDATA_RESPONSE_VALUE:
            raise _RconProtocolError(
                f"unexpected response type: {response_type}"
            )

    @staticmethod
    def _log_error(operation: str, error: Exception) -> None:
        print(f"RCON {operation} failed: {error}", file=sys.stderr)
