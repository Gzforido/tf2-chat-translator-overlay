"""No real credentials or user config are touched by these tests."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from utils import setup_api


class SetupApiTests(unittest.TestCase):
    def test_first_run_saves_keys_locally_without_echoing_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            output = io.StringIO()
            with (
                patch("utils.setup_api.CONFIG_PATH", config_path),
                patch("utils.config_loader.CONFIG_PATH", config_path),
                patch.object(sys, "argv", ["setup_api"]),
                patch(
                    "utils.setup_api.getpass.getpass",
                    side_effect=["sample-deepl-secret", "", "", ""],
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(setup_api.main(), 0)

            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["deepl_api_key"], "sample-deepl-secret")
            self.assertTrue(config["rcon_password"])
            self.assertNotEqual(config["rcon_password"], "tf2rcon")
            self.assertNotIn("sample-deepl-secret", output.getvalue())

    def test_check_does_not_create_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            with (
                patch("utils.setup_api.CONFIG_PATH", config_path),
                patch.object(sys, "argv", ["setup_api", "--check-required"]),
            ):
                self.assertEqual(setup_api.main(), 1)
            self.assertFalse(config_path.exists())


if __name__ == "__main__":
    unittest.main()
