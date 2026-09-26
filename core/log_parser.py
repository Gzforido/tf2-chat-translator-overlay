"""Разбор строк игрового чата из console.log Team Fortress 2."""

import re
import time
from dataclasses import dataclass
from typing import Optional


CHAT_SEPARATOR = " : "
TEAM_PREFIX = "(TEAM)"
DEAD_PREFIX = "*DEAD*"
STATUS_KEYWORDS = {
    "version",
    "udp/ip",
    "steamid",
    "account",
    "map",
    "tags",
    "players",
    "edicts",
    "hostname",
    "ip",
}
_TIMESTAMP_PREFIX = re.compile(
    r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\s+-\s+"
)


@dataclass
class ChatMessage:
    """Сообщение, извлечённое из игрового лога."""

    player_name: str
    text: str
    is_team: bool
    timestamp: float


def _remove_prefix(value: str, prefix: str) -> Optional[str]:
    """Удалить служебный префикс, только если за ним есть пробел."""
    if not value.startswith(prefix):
        return None

    remainder = value[len(prefix) :]
    if not remainder or not remainder[0].isspace():
        return None

    return remainder.lstrip()


def _parse_player_section(value: str) -> tuple[str, bool]:
    """Извлечь имя и командный режим из части строки перед разделителем."""
    player_name = value.strip()
    is_team = False

    # Цикл поддерживает любой порядок и повторение служебных префиксов.
    while player_name:
        without_team_prefix = _remove_prefix(player_name, TEAM_PREFIX)
        if without_team_prefix is not None:
            player_name = without_team_prefix
            is_team = True
            continue

        without_dead_prefix = _remove_prefix(player_name, DEAD_PREFIX)
        if without_dead_prefix is not None:
            player_name = without_dead_prefix
            continue

        break

    return player_name.strip(), is_team


def parse_line(line: str) -> Optional[ChatMessage]:
    """Преобразовать строку TF2 в сообщение чата или вернуть ``None``."""
    normalized_line = line.rstrip("\r\n")
    normalized_line = _TIMESTAMP_PREFIX.sub("", normalized_line, count=1)
    player_section, separator, message_text = normalized_line.partition(
        CHAT_SEPARATOR
    )
    if not separator:
        return None

    player_name, is_team = _parse_player_section(player_section)
    message_text = message_text.strip()

    if player_name.strip().lower() in STATUS_KEYWORDS:
        return None

    if not player_name or not message_text:
        return None

    return ChatMessage(
        player_name=player_name,
        text=message_text,
        is_team=is_team,
        timestamp=time.time(),
    )


if __name__ == "__main__":
    import unittest

    class ParseLineTests(unittest.TestCase):
        def assert_chat_message(
            self,
            line: str,
            expected_player_name: str,
            expected_text: str,
            expected_is_team: bool,
        ) -> None:
            message = parse_line(line)

            self.assertIsInstance(message, ChatMessage)
            assert message is not None
            self.assertEqual(message.player_name, expected_player_name)
            self.assertEqual(message.text, expected_text)
            self.assertEqual(message.is_team, expected_is_team)
            self.assertIsInstance(message.timestamp, float)
            self.assertGreater(message.timestamp, 0.0)

        def test_regular_chat_message(self) -> None:
            self.assert_chat_message(
                "Player123 : hello team",
                "Player123",
                "hello team",
                False,
            )

        def test_dead_chat_message(self) -> None:
            self.assert_chat_message(
                "*DEAD* Player123 : gg",
                "Player123",
                "gg",
                False,
            )

        def test_team_chat_message(self) -> None:
            self.assert_chat_message(
                "(TEAM) Player123 : rush mid",
                "Player123",
                "rush mid",
                True,
            )

        def test_team_dead_chat_message(self) -> None:
            self.assert_chat_message(
                "(TEAM) *DEAD* Player123 : help",
                "Player123",
                "help",
                True,
            )

        def test_timestamped_regular_chat_message(self) -> None:
            self.assert_chat_message(
                "12:34:56 - PlayerName : message",
                "PlayerName",
                "message",
                False,
            )

        def test_timestamped_dead_chat_message(self) -> None:
            self.assert_chat_message(
                "12:34:56 - *DEAD* PlayerName : gg",
                "PlayerName",
                "gg",
                False,
            )

        def test_timestamped_team_chat_message(self) -> None:
            self.assert_chat_message(
                "12:34:56 - (TEAM) PlayerName : rush",
                "PlayerName",
                "rush",
                True,
            )

        def test_timestamped_team_dead_chat_message(self) -> None:
            self.assert_chat_message(
                "12:34:56 - (TEAM) *DEAD* Player : help",
                "Player",
                "help",
                True,
            )

        def test_prefixes_in_reverse_order(self) -> None:
            self.assert_chat_message(
                "*DEAD* (TEAM) Player123 : incoming",
                "Player123",
                "incoming",
                True,
            )

        def test_repeated_prefixes(self) -> None:
            self.assert_chat_message(
                "*DEAD* (TEAM) *DEAD* Player123 : retry",
                "Player123",
                "retry",
                True,
            )

        def test_player_name_with_spaces_and_special_characters(self) -> None:
            self.assert_chat_message(
                "[Clan] Dr. Who!? #7 : medic please",
                "[Clan] Dr. Who!? #7",
                "medic please",
                False,
            )

        def test_separator_inside_message_text(self) -> None:
            self.assert_chat_message(
                "Player123 : meet at 12:30 : near spawn",
                "Player123",
                "meet at 12:30 : near spawn",
                False,
            )

        def test_line_ending_is_removed(self) -> None:
            self.assert_chat_message(
                "Player123 : good game\r\n",
                "Player123",
                "good game",
                False,
            )

        def test_marker_text_without_following_space_is_part_of_name(self) -> None:
            self.assert_chat_message(
                "*DEAD*pool : hello",
                "*DEAD*pool",
                "hello",
                False,
            )

        def test_non_chat_lines(self) -> None:
            lines = (
                "Player123 joined the game.",
                "Player123 left the game.",
                "Team Fortress",
                "Player123: missing spaces",
                "Player123 : ",
                " : message without a player",
                "",
            )

            for line in lines:
                with self.subTest(line=line):
                    self.assertIsNone(parse_line(line))

    unittest.main()
