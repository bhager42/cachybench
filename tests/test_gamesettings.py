"""Tests for the game-settings handlers.

The Cyberpunk fixture reproduces the shape of ``UserSettings.json`` as the 2.x
game writes it: a ``groups`` tree, each group holding ``options`` with
``name``/``type``/``value`` and, for list types, the accepted ``values``.
It is a reduced sample, not a copy of a real file.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cachybench import gamesettings  # noqa: E402
from cachybench.gamesettings import Cyberpunk2077, SettingsError  # noqa: E402

FIXTURE = {
    "version": 140,
    "data": [
        {
            "group_name": "/video/display",
            "options": [
                {"name": "VSync", "type": "string_list", "value": "UI-Settings-Video-QualitySetting-Off", "is_dynamic": True, "index": 0},
                {"name": "MaximumFPS_OnOff", "type": "bool", "value": False, "default_value": False},
                {"name": "MaximumFPS_Value", "type": "int", "value": 30, "default_value": 30, "min_value": 10, "max_value": 240, "step_value": 1},
                {"name": "WindowMode", "type": "string_list", "value": "BorderlessWindowed", "is_dynamic": True, "index": 1},
                {"name": "Resolution", "type": "string_list", "value": "3440x1440", "is_dynamic": True, "index": 23},
                {"name": "ReflexMode", "type": "string_list", "value": "Enabled", "is_dynamic": False, "index": 1, "default_index": 1, "values": ["Disabled", "Enabled", "Enabled + Boost"]},
            ],
        },
        {
            "group_name": "/graphics/basic",
            "options": [
                {"name": "FieldOfView", "type": "float", "value": 80.0, "default_value": 80.0, "min_value": 70.0, "max_value": 100.0, "step_value": 5.0},
                {"name": "MotionBlur", "type": "string_list", "value": "High", "is_dynamic": False, "index": 2, "default_index": 1, "values": ["Off", "Low", "High"]},
            ],
        },
        {
            "group_name": "/graphics/raytracing",
            "options": [
                {"name": "RayTracing", "type": "bool", "value": True, "default_value": False},
                {"name": "RayTracedLighting", "type": "string_list", "value": "Ultra", "is_dynamic": False, "index": 2, "default_index": 0, "values": ["Off", "Medium", "Ultra", "Psycho"]},
            ],
        },
        {
            "group_name": "/graphics/advanced",
            "options": [
                {"name": "Anisotropy", "type": "int_list", "value": 16, "is_dynamic": False, "index": 3, "default_index": 0, "values": [1, 4, 8, 16]},
                {"name": "Quality", "type": "string_list", "value": "High", "is_dynamic": False, "index": 1, "values": ["Low", "High"]},
            ],
        },
        {
            "group_name": "/graphics/performance",
            "options": [
                {"name": "CrowdDensity", "type": "name_list", "value": "High", "is_dynamic": False, "index": 2, "default_index": 2, "values": ["Low", "Medium", "High"]},
                {"name": "Quality", "type": "string_list", "value": "Auto", "is_dynamic": False, "index": 0, "values": ["Auto", "Balanced"]},
            ],
        },
    ],
}


class CyberpunkHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "UserSettings.json"
        self.path.write_text(json.dumps(FIXTURE, indent=4))
        self.handler = Cyberpunk2077()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reload(self) -> dict:
        return json.loads(self.path.read_text())

    def option(self, group_index: int, name: str) -> dict:
        return next(o for o in self.reload()["data"][group_index]["options"] if o["name"] == name)

    def test_read_covers_every_group_and_flags_dynamic_lists(self) -> None:
        options = {o.qualified: o for o in self.handler.read(self.path)}
        self.assertIn("/video/display/Resolution", options)
        self.assertIn("/graphics/raytracing/RayTracing", options)
        self.assertIn("/graphics/advanced/Quality", options)
        self.assertTrue(options["/video/display/Resolution"].dynamic)
        self.assertFalse(options["/graphics/basic/MotionBlur"].dynamic)
        self.assertEqual(options["/graphics/advanced/Anisotropy"].allowed, [1, 4, 8, 16])

    def test_apply_changes_value_and_index_only(self) -> None:
        self.handler.apply(
            self.path,
            {
                "Resolution": "2560x1440",
                "WindowMode": "Fullscreen",
                "RayTracing": False,
                "FieldOfView": 90,
                "MotionBlur": "off",
                "Anisotropy": 8,
                "RayTracedLighting": "Psycho",
            },
        )
        doc = self.reload()
        self.assertEqual(doc["version"], 140)
        self.assertEqual(self.option(0, "Resolution")["value"], "2560x1440")
        self.assertEqual(self.option(0, "Resolution")["index"], 23)  # dynamic: index untouched
        self.assertEqual(self.option(0, "WindowMode")["value"], "Fullscreen")
        self.assertIs(self.option(2, "RayTracing")["value"], False)
        self.assertIs(self.option(2, "RayTracing")["default_value"], False)  # untouched
        fov = self.option(1, "FieldOfView")
        self.assertEqual(fov["value"], 90.0)
        self.assertIsInstance(fov["value"], float)
        self.assertEqual(fov["step_value"], 5.0)
        blur = self.option(1, "MotionBlur")
        self.assertEqual((blur["value"], blur["index"]), ("Off", 0))  # canonical spelling, index in step
        self.assertEqual(blur["default_index"], 1)
        aniso = self.option(3, "Anisotropy")
        self.assertEqual((aniso["value"], aniso["index"]), (8, 2))
        lighting = self.option(2, "RayTracedLighting")
        self.assertEqual((lighting["value"], lighting["index"]), ("Psycho", 3))

    def test_vsync_localisation_key_shorthand(self) -> None:
        changes = self.handler.plan(self.path, {"VSync": "On"})
        self.assertEqual(changes[0].new, "UI-Settings-Video-QualitySetting-On")
        changes = self.handler.plan(self.path, {"VSync": False})
        self.assertEqual(changes[0].new, "UI-Settings-Video-QualitySetting-Off")
        changes = self.handler.plan(self.path, {"VSync": "UI-Settings-Video-QualitySetting-On"})
        self.assertEqual(changes[0].new, "UI-Settings-Video-QualitySetting-On")

    def test_dynamic_list_accepts_any_string(self) -> None:
        # No `values` in the file, so nothing to validate against until the game runs.
        changes = self.handler.plan(self.path, {"Resolution": "7680x4320"})
        self.assertEqual(changes[0].new, "7680x4320")

    def test_rejects_values_the_game_does_not_offer(self) -> None:
        with self.assertRaisesRegex(SettingsError, "not one of"):
            self.handler.plan(self.path, {"MotionBlur": "Ultra"})
        with self.assertRaisesRegex(SettingsError, "not one of"):
            self.handler.plan(self.path, {"Anisotropy": 2})
        with self.assertRaisesRegex(SettingsError, "above the maximum"):
            self.handler.plan(self.path, {"FieldOfView": 120})
        with self.assertRaisesRegex(SettingsError, "below the minimum"):
            self.handler.plan(self.path, {"MaximumFPS_Value": 5})
        with self.assertRaisesRegex(SettingsError, "true/false"):
            self.handler.plan(self.path, {"RayTracing": "sometimes"})
        with self.assertRaisesRegex(SettingsError, "expected a number"):
            self.handler.plan(self.path, {"FieldOfView": True})
        with self.assertRaisesRegex(SettingsError, "more than once"):
            self.handler.plan(self.path, {"RayTracing": True, "raytracing": False})

    def test_unknown_option_and_ambiguous_names(self) -> None:
        with self.assertRaisesRegex(SettingsError, "No option named"):
            self.handler.plan(self.path, {"Bloom": True})
        with self.assertRaisesRegex(SettingsError, "several groups"):
            self.handler.plan(self.path, {"Quality": "Auto"})
        changes = self.handler.plan(self.path, {"/graphics/performance/Quality": "Balanced"})
        self.assertEqual(changes[0].option.group, "/graphics/performance")
        self.assertEqual(changes[0].new, "Balanced")
        with self.assertRaisesRegex(SettingsError, "No option 'Quality' in group"):
            self.handler.plan(self.path, {"/graphics/basic/Quality": "High"})

    def test_case_insensitive_bare_name(self) -> None:
        changes = self.handler.plan(self.path, {"raytracing": False})
        self.assertEqual(changes[0].option.name, "RayTracing")

    def test_verify_reports_drift(self) -> None:
        wanted = {"Resolution": "2560x1440", "RayTracing": False}
        self.handler.apply(self.path, wanted)
        self.assertEqual(self.handler.verify(self.path, wanted), {})
        # Simulate the game resetting the resolution on exit.
        doc = self.reload()
        next(o for o in doc["data"][0]["options"] if o["name"] == "Resolution")["value"] = "3440x1440"
        self.path.write_text(json.dumps(doc))
        drift = self.handler.verify(self.path, wanted)
        self.assertEqual(drift, {"/video/display/Resolution": ("2560x1440", "3440x1440")})

    def test_managed_settings_restores_original(self) -> None:
        original = self.path.read_text()
        with gamesettings.managed_settings("cyberpunk2077", 1091500, {"RayTracing": False}, str(self.path)) as managed:
            managed.apply()
            self.assertIs(self.option(2, "RayTracing")["value"], False)
            self.assertTrue(self.path.with_name("UserSettings.json.cachybench-backup").exists())
        self.assertEqual(self.path.read_text(), original)
        self.assertFalse(self.path.with_name("UserSettings.json.cachybench-backup").exists())

    def test_managed_settings_restores_on_exception(self) -> None:
        original = self.path.read_text()
        with self.assertRaises(KeyboardInterrupt):
            with gamesettings.managed_settings("cyberpunk2077", 1091500, {"VSync": False}, str(self.path)) as managed:
                managed.apply()
                raise KeyboardInterrupt
        self.assertEqual(self.path.read_text(), original)

    def test_missing_file_is_explained(self) -> None:
        with self.assertRaisesRegex(SettingsError, "first launch"):
            self.handler.read(self.path.with_name("nope.json"))
        with self.assertRaisesRegex(SettingsError, "Run the game once"):
            with gamesettings.managed_settings("cyberpunk2077", 1091500, {"VSync": False}, str(self.path.with_name("nope.json"))):
                pass

    def test_unknown_handler(self) -> None:
        with self.assertRaisesRegex(SettingsError, "Unknown settings handler"):
            gamesettings.get_handler("doom")


class ConfigTests(unittest.TestCase):
    def test_settings_block_parses_and_validates(self) -> None:
        from cachybench import config

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "b.toml"
            cfg_path.write_text(
                '[[game]]\nname = "CP"\nappid = 1\n'
                '[game.settings]\nhandler = "cyberpunk2077"\nstrict = true\n'
                '[game.settings.values]\nVSync = false\nResolution = "2560x1440"\n'
            )
            cfg = config.load(cfg_path)
            game = cfg.games[0]
            assert game.settings is not None
            self.assertEqual(game.settings.handler, "cyberpunk2077")
            self.assertTrue(game.settings.strict)
            self.assertEqual(game.settings.values, {"VSync": False, "Resolution": "2560x1440"})

            cfg_path.write_text('[[game]]\nname = "CP"\nappid = 1\n[game.settings]\nhandler = "x"\n')
            with self.assertRaisesRegex(config.ConfigError, "values"):
                config.load(cfg_path)

            cfg_path.write_text('[[game]]\nname = "CP"\nappid = 1\n[game.settings]\nvalues = { A = 1 }\n')
            with self.assertRaisesRegex(config.ConfigError, "handler"):
                config.load(cfg_path)

            cfg_path.write_text(
                '[[game]]\nname = "CP"\nappid = 1\n[game.settings]\nhandler = "x"\nbogus = 1\nvalues = { A = 1 }\n'
            )
            with self.assertRaisesRegex(config.ConfigError, "unknown option"):
                config.load(cfg_path)


if __name__ == "__main__":
    unittest.main()
