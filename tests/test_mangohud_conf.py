"""The MangoHud.conf we write is the one thing the game actually reads."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cachybench import config, runners  # noqa: E402


class MangoHudConfTests(unittest.TestCase):
    def test_manual_mode_has_no_autostart(self) -> None:
        conf = runners.build_mangohud_conf(Path("/tmp/x"), autostart_s=0, duration_s=90)
        self.assertNotIn("autostart_log", conf)
        self.assertIn("log_duration=90\n", conf)

    def test_timed_mode_autostarts_after_warmup(self) -> None:
        conf = runners.build_mangohud_conf(Path("/tmp/x"), autostart_s=30, duration_s=90)
        self.assertIn("autostart_log=30\n", conf)

    def test_output_folder_is_absolute(self) -> None:
        conf = runners.build_mangohud_conf(Path("results/run1"), autostart_s=0, duration_s=90)
        line = next(l for l in conf.splitlines() if l.startswith("output_folder="))
        self.assertTrue(os.path.isabs(line.split("=", 1)[1]), line)

    def test_newest_log_skips_mangohud_summary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertIsNone(runners.newest_log(folder))
            main = folder / "Cyberpunk2077_2026-09-13_15-28-47.csv"
            main.write_text("fps,frametime\n60,16.6\n")
            summary = folder / "Cyberpunk2077_2026-09-13_15-28-47_summary.csv"
            summary.write_text("Average FPS\n60\n")
            # Summary is written last, so it is the newest by mtime.
            os.utime(main, (1_000_000, 1_000_000))
            os.utime(summary, (1_000_001, 1_000_001))
            self.assertEqual(runners.newest_log(folder), main)
            (folder / "only_summary_summary.csv").write_text("x")
            self.assertEqual(runners.newest_log(folder), main)

    def test_results_dir_is_made_absolute_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "b.toml"
            cfg_path.write_text('[defaults]\nresults_dir = "./results"\n[[game]]\nname = "G"\nappid = 1\n')
            cfg = config.load(cfg_path)
            self.assertTrue(os.path.isabs(cfg.defaults.results_dir))
            self.assertEqual(cfg.defaults.results_dir, os.path.abspath("./results"))


if __name__ == "__main__":
    unittest.main()
