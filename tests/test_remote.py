from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / "tools" / "engine_studies" / "workstation"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, REMOTE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


agent = load("ws_agent")
ctl = load("ws_ctl")


FIXTURE = textwrap.dedent(
    r"""
    import argparse, json, os, pathlib, time
    parser = argparse.ArgumentParser()
    parser.add_argument("subcommand")
    parser.add_argument("--root", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--ports")
    parser.add_argument("--backend")
    parser.add_argument("--jobs")
    parser.add_argument("--threads")
    parser.add_argument("--profile")
    parser.add_argument("--design")
    parser.add_argument("--axis")
    args = parser.parse_args()
    root = pathlib.Path(args.root)
    (root / "receipts").mkdir(parents=True, exist_ok=True)
    (root / "runs" / "fixture").mkdir(parents=True, exist_ok=True)
    def put(name, value):
        temp = root / (name + ".tmp")
        temp.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temp, root / name)
    put("status.json", {"running": True, "state": "running", "subcommand": args.subcommand, "pid": os.getpid()})
    (root / "runs" / "fixture" / "log.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    for _ in range(30):
        if pathlib.Path(args.stop_file).exists():
            put("status.json", {"running": False, "state": "stopped", "stopped_at": time.time(), "exit_code": 0})
            raise SystemExit(0)
        time.sleep(.02)
    put("env.json", {"fixture": True})
    (root / "receipts" / "fixture.json").write_text(json.dumps({"case": "fixture"}), encoding="utf-8")
    put("status.json", {"running": False, "state": "finished", "exit_code": 0})
    """
)


class LiveAgent(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-ws-remote-")
        self.base = Path(self.temp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        self.fixture = self.base / "validator.py"
        self.fixture.write_text(FIXTURE, encoding="utf-8")
        self.token = "test-token-value-0123456789abcdef"
        self.token_file = self.base / "token.txt"
        self.token_file.write_text(self.token + "\n", encoding="utf-8")
        self.state = agent.AgentState(self.root, self.token, self.fixture)
        self.server = agent.AgentServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = ctl.Client(self.url, self.token, 3)

    def tearDown(self):
        child = self.state.child
        if child and child.poll() is None:
            self.state._kill_tree(child.pid)
            child.wait(timeout=3)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request_status(self, path: str, token: str | None = None) -> int:
        request = Request(self.url + path, headers={"Authorization": "Bearer " + (token or self.token)})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status
        except HTTPError as exc:
            return exc.code

    def wait_terminal(self, expected: str = "finished") -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = self.client.request("GET", "/status")
            if value.get("state") == expected:
                return value
            time.sleep(.02)
        self.fail(f"validator did not reach {expected}")

    def test_auth_allowlist_and_traversal(self):
        self.assertEqual(self.request_status("/health", "wrong"), 401)
        self.assertEqual(self.request_status("/artifact/%2e%2e/config.json"), 403)
        (self.root / "config.json").write_text('{"secret":true}', encoding="utf-8")
        self.assertEqual(self.request_status("/artifact/config.json"), 403)
        (self.root / "summary.json").write_text('{"public":true}', encoding="utf-8")
        self.assertEqual(self.request_status("/artifact/summary.json"), 200)
        for name in ("gates.json", "matrix_plan.json"):
            (self.root / name).write_text('{"public":true}', encoding="utf-8")
            self.assertEqual(self.request_status(f"/artifact/{name}"), 200)

    def test_strict_start_actual_conflict_and_receipt(self):
        started = self.client.request("POST", "/start", {"subcommand": "env", "args": {}})
        self.assertGreater(started["pid"], 0)
        with self.assertRaises(ctl.ClientError) as conflict:
            self.client.request("POST", "/start", {"subcommand": "env", "args": {}})
        self.assertEqual(conflict.exception.status, 409)
        self.wait_terminal()
        listing = self.client.request("GET", "/receipts")
        self.assertEqual([item["name"] for item in listing["receipts"]], ["fixture.json"])
        receipt = self.client.request("GET", "/receipt/fixture.json", raw=True)
        self.assertEqual(json.loads(receipt), {"case": "fixture"})
        with self.assertRaises(ctl.ClientError) as invalid:
            self.client.request("POST", "/start", {"subcommand": "env", "args": {"code": "x"}})
        self.assertEqual(invalid.exception.status, 400)

    def test_config_provisioned_design_label_only(self):
        (self.root / "config.json").write_text(
            json.dumps({"designs": {"heldout-board": {"spd": "external.spd"}}}),
            encoding="utf-8",
        )
        command = self.state._command_for("baseline", {"design": "heldout-board"})
        self.assertEqual(command[-2:], ["--design", "heldout-board"])
        with self.assertRaises(agent.RequestFailure):
            self.state._command_for("baseline", {"design": "not-configured"})
        with self.assertRaises(agent.RequestFailure):
            self.state._command_for("baseline", {"design": "../escape"})

    def test_graceful_stop_and_log_tail(self):
        self.client.request("POST", "/start", {"subcommand": "env", "args": {}})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.client.request("GET", "/status").get("state") != "running":
            time.sleep(.01)
        answer = self.client.request("POST", "/stop", {"mode": "graceful"})
        self.assertTrue(answer["accepted"])
        self.wait_terminal("stopped")
        log = self.client.request("GET", "/log?tail=2")
        self.assertEqual(log["lines"], ["two", "three"])

    def test_now_stop_allows_runner_to_record_stopped_status(self):
        self.client.request("POST", "/start", {"subcommand": "env", "args": {}})
        answer = self.client.request("POST", "/stop", {"mode": "now"})
        self.assertTrue(answer["accepted"])
        status = self.wait_terminal("stopped")
        self.assertIsNotNone(status.get("stopped_at"))
        self.assertFalse(status["running"])

    def test_fetch_is_atomic_and_refuses_overwrite(self):
        (self.root / "summary.json").write_text('{"done":true}', encoding="utf-8")
        data = self.client.request("GET", "/artifact/summary.json", raw=True)
        out = self.base / "downloads"
        destination = ctl._atomic_no_overwrite(data, out, ctl.PurePosixPath("summary.json"))
        self.assertEqual(destination.read_bytes(), data)
        with self.assertRaises(ctl.ClientError) as duplicate:
            ctl._atomic_no_overwrite(data, out, ctl.PurePosixPath("summary.json"))
        self.assertEqual(duplicate.exception.exit_code, ctl.EXIT_CONFLICT)


class CommandLineTests(unittest.TestCase):
    def test_remote_protocol_version(self):
        self.assertEqual(agent.VERSION, "1.0.1")

    def test_real_ws_validate_env_through_agent(self):
        if importlib.util.find_spec("spd_pi_engine") is None:
            self.skipTest("the engine is not installed in this Python")
        validator = REMOTE / "ws_validate.py"
        if not validator.is_file():
            self.skipTest("ws_validate.py is not present")
        with tempfile.TemporaryDirectory(prefix="test-ws-real-env-") as raw:
            root = Path(raw)
            data = root / "data"
            data.mkdir()
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "engine_python": sys.executable,
                        "engine_root": str(ROOT),
                        "data_dir": str(data),
                        "designs": {
                            "design_a": {
                                "spd": "design_a.spd",
                                "reference": "design_a_Zdiag.npz",
                                "family": "package",
                            }
                        },
                        "port_sets": {
                            "P9": {"design_a": ["PortA"]},
                            "P20": {"design_a": ["PortA"]},
                            "P92": {"design_a": "all"},
                            "E4": {"design_a": ["PortA"]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            token = "real-env-token-0123456789abcdef"
            state = agent.AgentState(root, token, validator)
            server = agent.AgentServer(("127.0.0.1", 0), state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = ctl.Client(f"http://127.0.0.1:{server.server_address[1]}", token, 5)
            try:
                started = client.request("POST", "/start", {"subcommand": "env", "args": {}})
                self.assertGreater(started["pid"], 0)
                deadline = time.monotonic() + 25
                status = {}
                while time.monotonic() < deadline:
                    status = client.request("GET", "/status")
                    if status.get("subcommand") == "env" and status.get("running") is False:
                        break
                    time.sleep(.05)
                self.assertFalse(status.get("running"), status)
                self.assertEqual(status.get("subcommand"), "env")
                self.assertIn(status.get("exit_code"), (0, 1))
                env = client.request("GET", "/env")
                self.assertIsInstance(env.get("env"), dict)
                self.assertIn("hard_versions", env["env"])
                receipts = client.request("GET", "/receipts")
                self.assertEqual(receipts, {"receipts": []})
            finally:
                child = state.child
                if child and child.poll() is None:
                    state._kill_tree(child.pid)
                    child.wait(timeout=5)
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_powershell_bom_token_files(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "token.txt"
            value = "token-from-powershell-0123456789ab"
            path.write_bytes(b"\xef\xbb\xbf" + value.encode() + b"\r\n")
            self.assertEqual(agent.read_token(path), value)
            self.assertEqual(ctl.read_token(path), value)

    def test_agent_rejects_tokens_shorter_than_32_characters(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "token.txt"
            path.write_text("x" * 31, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "32"):
                agent.read_token(path)

    def test_client_refuses_redirect_before_forwarding_auth(self):
        class RedirectHandler(agent.http.server.BaseHTTPRequestHandler):
            seen = None

            def do_GET(self):
                type(self).seen = self.headers.get("Authorization")
                if self.path == "/first":
                    self.send_response(302)
                    self.send_header("Location", "/second")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"followed":true}')

            def log_message(self, *args):
                pass

        server = agent.http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            secret = "redirect-secret-0123456789abcdef"
            client = ctl.Client(f"http://127.0.0.1:{server.server_address[1]}", secret, 3)
            with self.assertRaises(ctl.ClientError) as raised:
                client.request("GET", "/first")
            self.assertEqual(raised.exception.status, 302)
            self.assertEqual(RedirectHandler.seen, "Bearer " + secret)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_agent_self_check_covers_round_trip(self):
        result = subprocess.run(
            [sys.executable, str(REMOTE / "ws_agent.py"), "--self-check"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["checks"]["traversal"], 403)
        self.assertTrue(payload["checks"]["duplicate"])
        self.assertTrue(payload["checks"]["real_env"])

    def test_watch_exit_codes_are_distinct(self):
        self.assertEqual(ctl._terminal({"running": False, "state": "stopped", "stopped_at": "now"}), ctl.EXIT_STOPPED)
        self.assertEqual(ctl._terminal({"running": False, "state": "failed", "exit_code": 4}), ctl.EXIT_FAILED)
        self.assertEqual(ctl._terminal({"running": False, "state": "finished", "exit_code": 0}), 0)
        self.assertNotEqual(ctl.EXIT_STOPPED, ctl.EXIT_WATCH_NETWORK)
        self.assertNotEqual(ctl.EXIT_FAILED, ctl.EXIT_WATCH_NETWORK)


class ConnectionLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-ws-connections-")
        self.root = Path(self.temp.name)
        self.token = "connection-test-token-0123456789ab"
        self.state = agent.AgentState(self.root, self.token, REMOTE / "ws_validate.py")
        self.baseline_threads = threading.active_count()
        self.server = agent.AgentServer(
            ("127.0.0.1", 0), self.state, max_active=32, request_timeout=0.35,
            log_max_bytes=320, log_backups=2,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.sockets: list[socket.socket] = []

    def tearDown(self):
        for connection in self.sockets:
            try:
                connection.close()
            except OSError:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_production_handler_timeout_is_30_seconds(self):
        self.assertEqual(agent.AgentHandler.timeout, 30)
        self.assertEqual(agent.DEFAULT_MAX_ACTIVE, 32)

    def test_half_open_connections_are_bounded_and_reclaimed(self):
        address = self.server.server_address
        started = time.monotonic()
        for _ in range(40):
            connection = socket.create_connection(address, timeout=1)
            self.sockets.append(connection)
        request = Request(self.url + "/health", headers={"Authorization": "Bearer " + self.token})
        try:
            with urlopen(request, timeout=4) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        self.assertIn(status, (200, 503))
        self.assertLess(time.monotonic() - started, 5)
        self.assertLessEqual(self.server.active_count, 32)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.server.active_count:
            time.sleep(.03)
        self.assertEqual(self.server.active_count, 0)
        self.assertLessEqual(threading.active_count(), self.baseline_threads + 2)
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)

    def test_oversize_body_is_rejected_without_being_read(self):
        request = Request(
            self.url + "/start",
            data=b"{}",
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Content-Length": str(agent.MAX_BODY + 1),
            },
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 413)

    def test_access_logs_redact_query_values_and_rotate(self):
        secret = "query-value-must-not-appear"
        for index in range(30):
            path = f"/log?tail=1&probe={secret}-{index}"
            self.assertEqual(self._status(path), 400)
        logs = sorted((self.root / "agent-logs").glob("agent.log*"))
        self.assertGreaterEqual(len(logs), 2)
        self.assertLessEqual(len(logs), 3)
        content = "".join(path.read_text(encoding="utf-8") for path in logs)
        self.assertNotIn(secret, content)
        self.assertIn("probe", content)
        self.assertLessEqual(max(path.stat().st_size for path in logs), 500)

    def _status(self, path: str) -> int:
        request = Request(self.url + path, headers={"Authorization": "Bearer " + self.token})
        try:
            with urlopen(request, timeout=2) as response:
                return response.status
        except HTTPError as exc:
            return exc.code


if __name__ == "__main__":
    unittest.main()
