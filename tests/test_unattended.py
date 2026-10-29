"""The parts that matter when nobody is watching the session."""

from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cachybench import cli, notify, runners  # noqa: E402
from cachybench.config import Config, Defaults, GameTarget  # noqa: E402


def write_frame_log(folder: Path, frames: int = 600) -> Path:
    path = folder / "Game_2026-01-01_00-00-00.csv"
    lines = ["fps,frametime,elapsed"]
    lines += [f"100,10,{i * 10_000_000}" for i in range(frames)]
    path.write_text("\n".join(lines) + "\n")
    return path


class GameSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.install_dir = root / "game"
        self.install_dir.mkdir()
        defaults = Defaults(
            runs=3, cooldown_s=0, warmup_s=0, log_duration_s=60,
            manual_start_timeout_s=120, results_dir=str(root / "results"),
        )
        self.cfg = Config(defaults=defaults, games=[], synthetics=[], path=root / "b.toml")
        with mock.patch.object(cli.sysinfo, "collect", return_value={}), mock.patch.object(
            cli.sysinfo, "warnings", return_value=[]
        ):
            self.session = cli.Session(root / "results", self.cfg.path)

        patches = [
            mock.patch.object(runners, "app_install_dir", return_value=self.install_dir),
            mock.patch.object(runners, "launch_steam_app"),
            mock.patch.object(runners, "wait_for_start", return_value={1}),
            mock.patch.object(runners, "terminate_game"),
            mock.patch.object(cli.notify, "send"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def manifest(self) -> dict:
        return json.loads(self.session.manifest.read_text())

    def test_runs_finished_before_an_interrupt_reach_session_json(self) -> None:
        calls = []

        def fake_wait(folder: Path, timeout_s: int) -> Path:
            calls.append(folder)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return write_frame_log(folder)

        target = GameTarget(name="Game", appid=1, mode="timed")
        with mock.patch.object(cli, "wait_for_log", side_effect=fake_wait):
            with self.assertRaises(cli.Interrupted):
                cli.run_game_target(target, self.cfg, self.session, lambda *a: None)

        results = self.manifest()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual([r["index"] for r in results[0]["runs"]], [1])
        self.assertEqual(results[0]["aggregate"]["run_count"], 1)

    def test_manual_run_without_keypress_skips_the_rest_quickly(self) -> None:
        target = GameTarget(name="Game", appid=1, mode="manual")
        with mock.patch.object(cli, "wait_for_log", return_value=None) as wait:
            result = cli.run_game_target(target, self.cfg, self.session, lambda *a: None)

        self.assertEqual(wait.call_count, 1)
        self.assertEqual(wait.call_args.args[1], 120 + 60 + 30)
        self.assertTrue(any("skipping the remaining 2 run(s)" in e for e in result["errors"]))
        self.assertEqual(self.manifest()["results"][0]["errors"], result["errors"])


class _Recorder(http.server.BaseHTTPRequestHandler):
    received: list[tuple[dict, str]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        _Recorder.received.append((dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class NotifyTests(unittest.TestCase):
    def test_push_posts_title_and_message(self) -> None:
        server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/topic"

        with mock.patch.dict("os.environ", {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            self.assertTrue(notify.push(url, "cachybench – done", "3 target(s)", urgent=True))

        headers, body = _Recorder.received[-1]
        self.assertEqual(body, "3 target(s)")
        self.assertEqual(headers["Title"], "cachybench  done")
        self.assertEqual(headers["Priority"], "high")

    def test_unreachable_endpoint_does_not_raise(self) -> None:
        self.assertFalse(notify.push("http://127.0.0.1:9/nothing", "t", "m"))
        self.assertFalse(notify.push("not a url", "t", "m"))


if __name__ == "__main__":
    unittest.main()
