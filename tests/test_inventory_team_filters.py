"""Regression tests for lobby-derived teams and inventory filters."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core.inventory_scanner import PlayerInventory
from core.rcon_client import RconClient, STATUS_CACHE_SECONDS, STEAMID64_BASE
from main import ApplicationController
from ui.inventory_overlay import InventoryOverlay


LOBBY = """CTFLobbyShared: ID:00028dcb1b567211  3 member(s), 0 pending
  Member[0] [U:1:100]  team = TF_GC_TEAM_INVADERS  type = MATCH_PLAYER
  Member[1] [U:1:200]  team = TF_GC_TEAM_INVADERS  type = MATCH_PLAYER
  Member[2] [U:1:300]  team = TF_GC_TEAM_DEFENDERS  type = MATCH_PLAYER
"""

STATUS = """hostname: Valve Matchmaking Server
version : 10828683/24 10828683 secure
players : 3 humans, 0 bots (32 max)
# userid name                uniqueid            connected ping loss state
# 1 "LocalPlayer" [U:1:100] 00:50 30 0 active
# 2 "Ally" [U:1:200] 01:00 40 0 active
# 3 "Enemy" [U:1:300] 01:10 50 0 active
"""


class _LogOnlyRconClient(RconClient):
    def __init__(self, log_path: Path) -> None:
        super().__init__()
        self.log_path = log_path
        self.emit_status = True
        self.status_output = STATUS
        self.lobby_response = ""

    def execute(self, command: str) -> str:
        if command == "status" and self.emit_status:
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(self.status_output)
        if command == "tf_lobby_debug":
            return self.lobby_response
        return ""


class LobbyTeamTests(unittest.TestCase):
    def test_manual_lobby_output_before_status_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "console.log"
            log_path.write_text("Connected to server\n" + LOBBY, encoding="utf-8")
            client = _LogOnlyRconClient(log_path)
            with patch("core.rcon_client.STATUS_QUIET_SECONDS", 0):
                players = client.get_players(log_path)

        self.assertIsNotNone(players)
        teams = {player["name"]: player["team"] for player in players}
        self.assertEqual(
            teams,
            {"LocalPlayer": "Invaders", "Ally": "Invaders", "Enemy": "Defenders"},
        )

    def test_lobby_from_previous_match_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "console.log"
            log_path.write_text(
                "Connected to old-server\n" + LOBBY
                + "Disconnect: changing server\nConnected to new-server\n",
                encoding="utf-8",
            )
            client = _LogOnlyRconClient(log_path)
            with patch("core.rcon_client.STATUS_QUIET_SECONDS", 0), patch(
                "core.rcon_client.LOBBY_WAIT_SECONDS", 0
            ):
                players = client.get_players(log_path)

        self.assertIsNotNone(players)
        self.assertEqual([player["team"] for player in players], ["", "", ""])

    def test_incomplete_lobby_snapshot_is_rejected(self) -> None:
        self.assertEqual(RconClient._parse_lobby_teams(LOBBY[:-80]), {})

    def test_missing_status_keeps_cached_roster_without_lobby_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "console.log"
            log_path.write_text("Connected to server\n", encoding="utf-8")
            client = _LogOnlyRconClient(log_path)
            client.lobby_response = LOBBY
            with patch("core.rcon_client.STATUS_QUIET_SECONDS", 0):
                first = client.get_players(log_path)
            self.assertEqual(len(first), 3)

            client.emit_status = False
            client.lobby_response = ""
            with patch("core.rcon_client.STATUS_WAIT_SECONDS", 0), patch(
                "core.rcon_client.LOBBY_WAIT_SECONDS", 0
            ):
                second = client.get_players(log_path)
                self.assertEqual(second, first)
                client._cached_players_at = (
                    time.monotonic() - STATUS_CACHE_SECONDS - 1
                )
                self.assertIsNone(client.get_players(log_path))

    def test_confirmed_empty_status_clears_cached_roster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "console.log"
            log_path.write_text("Connected to server\n", encoding="utf-8")
            client = _LogOnlyRconClient(log_path)
            with patch("core.rcon_client.STATUS_QUIET_SECONDS", 0):
                self.assertEqual(len(client.get_players(log_path)), 3)
                client.status_output = (
                    "version : 10828683/24 10828683 secure\n"
                    "players : 0 humans, 0 bots (32 max)\n"
                )
                self.assertEqual(client.get_players(log_path), [])
        self.assertEqual(client._cached_players, [])


class RefreshStabilityTests(unittest.TestCase):
    def test_unavailable_status_does_not_remove_players(self) -> None:
        refresh_running = threading.Event()
        refresh_running.set()
        controller = SimpleNamespace(
            _active_player_ids_lock=threading.Lock(),
            _inventory_session_generation=0,
            _active_player_ids={"existing-player"},
            _shutting_down=threading.Event(),
            _inventory_refresh_running=refresh_running,
            _get_rcon_players=lambda: None,
        )

        ApplicationController._refresh_inventories_worker(controller)

        self.assertEqual(controller._active_player_ids, {"existing-player"})
        self.assertFalse(refresh_running.is_set())

    def test_new_server_connection_clears_previous_match(self) -> None:
        actions: list[str] = []
        controller = SimpleNamespace(
            _shutting_down=threading.Event(),
            _active_player_ids_lock=threading.Lock(),
            _inventory_session_generation=0,
            _active_player_ids={"old-player"},
            _rcon_client=SimpleNamespace(
                clear_player_cache=lambda: actions.append("clear-cache")
            ),
            _inventory_overlay=SimpleNamespace(
                set_local_team=lambda team: actions.append(f"team:{team}"),
                clear_players=lambda: actions.append("clear-rows"),
            ),
        )

        ApplicationController.on_log_line(
            controller, "Connected to test-server:27015"
        )

        self.assertEqual(controller._active_player_ids, set())
        self.assertEqual(controller._inventory_session_generation, 1)
        self.assertEqual(actions, ["clear-cache", "team:", "clear-rows"])


class OverlayFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_team_filters_preserve_minimum_price(self) -> None:
        overlay = InventoryOverlay()
        try:
            for account_id, name, value in (
                (100, "LocalPlayer", 80.0),
                (200, "Ally", 130.0),
                (300, "Enemy", 120.0),
            ):
                steamid = str(STEAMID64_BASE + account_id)
                overlay.update_player(
                    PlayerInventory(
                        steamid=steamid,
                        player_name=name,
                        team="",
                        total_value_usd=value,
                        item_count=10,
                        fetched_at=time.time(),
                    )
                )

            overlay.set_player_teams(
                {
                    str(STEAMID64_BASE + 100): "Invaders",
                    str(STEAMID64_BASE + 200): "Invaders",
                    str(STEAMID64_BASE + 300): "Defenders",
                }
            )
            overlay.set_local_team("Invaders")

            def visible_names() -> set[str]:
                return {
                    overlay._players[steamid].player_name
                    for steamid, row in overlay._row_widgets.items()
                    if not row.widget.isHidden()
                }

            overlay.set_filters("allies", 100.0)
            self.assertEqual(visible_names(), {"Ally"})
            overlay.set_filters("enemies", 100.0)
            self.assertEqual(visible_names(), {"Enemy"})
            overlay.set_filters("allies", 0.0)
            self.assertEqual(visible_names(), {"LocalPlayer", "Ally"})
            overlay.set_player_teams({steamid: "" for steamid in overlay._players})
            self.assertEqual(visible_names(), set())
        finally:
            overlay.close()


if __name__ == "__main__":
    unittest.main()
