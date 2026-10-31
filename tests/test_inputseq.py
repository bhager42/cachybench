"""Scripted input: the config grammar, the replay, and the scripted run mode."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cachybench import cli, config, inputseq, runners  # noqa: E402
from cachybench.config import Config, Defaults, GameTarget  # noqa: E402

from test_unattended import write_frame_log  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_grammar(self) -> None:
        steps = inputseq.parse(["wait 2.5", "key Enter", "key down x3", "key alt+enter", "log"])
        self.assertEqual(
            [s.describe() for s in steps],
            ["wait 2.5s", "enter", "down x3", "alt+enter", "start logging (Shift+F2)"],
        )

    def test_rejects_typos_with_the_step_number(self) -> None:
        for bad in (["wiat 3"], ["key entr"], ["wait soon"], ["key down x0"], ["wait -1"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(inputseq.SequenceError, "step 1"):
                inputseq.parse(bad)

    def test_rejects_non_strings(self) -> None:
        with self.assertRaises(inputseq.SequenceError):
            inputseq.parse([3])  # type: ignore[list-item]


class PlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[list[str]] = []
        patch = mock.patch.object(inputseq, "HOLD_S", 0)
        patch.start()
        self.addCleanup(patch.stop)

    def play(self, steps: list[str], alive=lambda: True) -> list[float]:
        slept: list[float] = []
        inputseq.play(inputseq.parse(steps), alive, send=self.sent.append, sleep=slept.append)
        return slept

    def test_keys_are_pressed_and_released_in_order(self) -> None:
        self.play(["key alt+enter", "key down x2", "log"])
        self.assertEqual(
            self.sent,
            [
                ["56:1", "28:1"], ["28:0", "56:0"],
                ["108:1"], ["108:0"], ["108:1"], ["108:0"],
                ["42:1", "60:1"], ["60:0", "42:0"],
            ],
        )

    def test_long_waits_are_sliced(self) -> None:
        slept = self.play(["wait 5"])
        self.assertEqual(slept, [2.0, 2.0, 1.0])

    def test_stops_if_the_game_exits_mid_wait(self) -> None:
        with self.assertRaisesRegex(inputseq.SequenceError, "exited during step 1"):
            self.play(["wait 30", "key enter"], alive=lambda: False)
        self.assertEqual(self.sent, [])

    def test_release_is_sent_even_when_interrupted(self) -> None:
        def sleep_then_interrupt(_: float) -> None:
            raise KeyboardInterrupt

        with mock.patch.object(inputseq.time, "sleep", sleep_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                inputseq.press(("leftshift", "f2"), self.sent.append)
        self.assertEqual(self.sent, [["42:1", "60:1"], ["60:0", "42:0"]])


class ConfigTests(unittest.TestCase):
    def load(self, game: str) -> config.Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.toml"
            path.write_text("[[game]]\nname = 'G'\nappid = 1\n" + textwrap.dedent(game))
            return config.load(path)

    def test_scripted_game_loads(self) -> None:
        cfg = self.load('mode = "scripted"\ninput_sequence = ["wait 1", "log"]\n')
        self.assertEqual(len(cfg.games[0].steps), 2)

    def test_scripted_game_needs_a_log_step(self) -> None:
        with self.assertRaisesRegex(config.ConfigError, "'log' step"):
            self.load('mode = "scripted"\ninput_sequence = ["wait 1"]\n')

    def test_log_step_only_in_scripted_mode(self) -> None:
        with self.assertRaisesRegex(config.ConfigError, "only belong"):
            self.load('mode = "manual"\ninput_sequence = ["log"]\n')

    def test_bad_step_is_a_config_error(self) -> None:
        with self.assertRaisesRegex(config.ConfigError, "unknown key"):
            self.load('mode = "scripted"\ninput_sequence = ["key nope", "log"]\n')


class ScriptedRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        defaults = Defaults(runs=3, cooldown_s=0, log_duration_s=60, results_dir=str(root / "r"))
        self.cfg = Config(defaults=defaults, games=[], synthetics=[], path=root / "b.toml")
        with mock.patch.object(cli.sysinfo, "collect", return_value={}), mock.patch.object(
            cli.sysinfo, "warnings", return_value=[]
        ):
            self.session = cli.Session(root / "r", self.cfg.path)
        self.target = GameTarget(
            name="G", appid=1, mode="scripted", input_sequence=["wait 1", "key enter", "log"]
        )
        self.played: list = []
        for patch in (
            mock.patch.object(runners, "app_install_dir", return_value=root),
            mock.patch.object(runners, "launch_steam_app"),
            mock.patch.object(runners, "wait_for_start", return_value={1}),
            mock.patch.object(runners, "terminate_game"),
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/ydotool"),
            mock.patch.object(cli.inputseq, "ydotool_socket", return_value=root / "s"),
            mock.patch.object(cli.inputseq, "play", side_effect=lambda steps, **kw: self.played.append(steps)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_each_run_replays_the_sequence_and_waits_only_for_the_capture(self) -> None:
        conf_calls = []
        with mock.patch.object(cli, "wait_for_log", side_effect=lambda d, t: write_frame_log(d)) as wait:
            result = cli.run_game_target(self.target, self.cfg, self.session, lambda *a: conf_calls.append(a))

        self.assertEqual(len(result["runs"]), 3)
        self.assertEqual(len(self.played), 3)
        self.assertEqual(wait.call_args.args[1], 60 + 60)
        # Logging waits for the sequence's Shift+F2 rather than autostarting.
        self.assertTrue(all(call[1] == 0 for call in conf_calls))

    def test_no_log_skips_the_remaining_runs(self) -> None:
        with mock.patch.object(cli, "wait_for_log", return_value=None):
            result = cli.run_game_target(self.target, self.cfg, self.session, lambda *a: None)
        self.assertEqual(len(self.played), 1)
        self.assertTrue(any("skipping the remaining 2" in e for e in result["errors"]))

    def test_sequence_failure_fails_the_run_not_the_session(self) -> None:
        with mock.patch.object(
            cli.inputseq, "play", side_effect=inputseq.SequenceError("game exited during step 1")
        ), mock.patch.object(cli, "wait_for_log") as wait:
            result = cli.run_game_target(self.target, self.cfg, self.session, lambda *a: None)
        wait.assert_not_called()
        self.assertEqual(len([e for e in result["errors"] if "exited" in e]), 3)

    def test_missing_ydotool_abandons_before_launching(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=None):
            result = cli.run_game_target(self.target, self.cfg, self.session, lambda *a: None)
        runners.launch_steam_app.assert_not_called()
        self.assertIn("ydotool", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
