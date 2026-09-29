"""Authenticated, fixed-function HTTP controller for workstation validation.

This module deliberately exposes no generic command, path, environment, or config
mutation API.  ``ws_validate.py`` remains the owner of status.json and all study
results; this process only starts one validator and serves selected results.
"""

from __future__ import annotations

import argparse
from collections import deque
import contextlib
import datetime as dt
import hmac
import http.server
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


VERSION = "1.1.0"
MAX_BODY = 64 * 1024
MAX_TAIL = 10_000
MIN_TOKEN_LENGTH = 32
DEFAULT_MAX_ACTIVE = 32
DEFAULT_LOG_MAX_BYTES = 1_048_576
DEFAULT_LOG_BACKUPS = 3
ACTIVE_WORDS = {"running", "starting", "stopping", "active", "in_progress"}
SUBCOMMANDS = {"baseline", "matrix", "converge", "report", "gates", "env"}
ARGUMENTS: dict[str, tuple[str, Any]] = {
    "ports": ("--ports", {"P9", "P92", "P20"}),
    "backend": ("--backend", {"splu", "auto", "cudss"}),
    "jobs": ("--jobs", lambda value: _bounded_int(value, "jobs", 1, 64)),
    "threads": ("--threads", lambda value: _bounded_int(value, "threads", 1, 64)),
    "profile": ("--profile", lambda value: _label(value, "profile")),
    "design": ("--design", None),
    "axis": ("--axis", {"B", "C", "D", "E", "all"}),
}
PUBLIC_FILES = {"summary.json", "program_validation_summary.json", "W15_REPORT.md",
                "gates.json", "matrix_plan.json"}
PUBLIC_DIRECTORIES = {"figures"}
PUBLIC_SUFFIXES = {".png", ".svg", ".pdf", ".json", ".csv"}
PRIVATE_ARTIFACT_NAMES = {"config.json", "token", "token.txt", ".token"}
RECEIPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.json\Z")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class RequestFailure(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _label(value: Any, name: str) -> str:
    if not isinstance(value, str) or not LABEL_RE.fullmatch(value):
        raise ValueError(f"{name} must match {LABEL_RE.pattern}")
    return value


def read_token(path: Path) -> str:
    try:
        # Windows PowerShell 5.1's ``Set-Content -Encoding UTF8`` emits a BOM.
        token = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise ValueError(f"cannot read token file {path}: {exc}") from exc
    if not token or "\n" in token or "\r" in token:
        raise ValueError("token file must contain one non-empty token")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"token must contain at least {MIN_TOKEN_LENGTH} characters")
    return token


def _json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"error": f"cannot read {path.name}: {exc}"}
    return value if isinstance(value, dict) else {"error": f"{path.name} is not a JSON object"}


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    flag = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or bool(flag & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _safe_existing(root: Path, relative: str, *, public_artifact: bool = False) -> Path:
    decoded = unquote(relative)
    if not decoded or "\\" in decoded or "\x00" in decoded:
        raise RequestFailure(403, "invalid relative path")
    pure = PurePosixPath(decoded)
    if pure.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        raise RequestFailure(403, "path escapes the configured root")
    if public_artifact:
        parts = pure.parts
        if any(part.lower() in PRIVATE_ARTIFACT_NAMES or "token" in part.lower() for part in parts):
            raise RequestFailure(403, "artifact is not public")
        allowed = (len(parts) == 1 and parts[0] in PUBLIC_FILES) or (
            len(parts) >= 2
            and parts[0] in PUBLIC_DIRECTORIES
            and pure.suffix.lower() in PUBLIC_SUFFIXES
        )
        if not allowed:
            raise RequestFailure(403, "artifact is not public")
    candidate = root.joinpath(*pure.parts)
    cursor = root
    for part in pure.parts:
        cursor = cursor / part
        if cursor.exists() and _is_reparse(cursor):
            raise RequestFailure(403, "reparse points are not served")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except FileNotFoundError as exc:
        raise RequestFailure(404, "file not found") from exc
    except (OSError, ValueError) as exc:
        raise RequestFailure(403, "path escapes the configured root") from exc
    if not resolved.is_file():
        raise RequestFailure(404, "file not found")
    return resolved


def _status_active(status: dict[str, Any] | None) -> bool:
    if not status:
        return False
    if status.get("running") is True:
        return True
    for key in ("state", "status", "phase"):
        value = status.get(key)
        if isinstance(value, str) and value.lower() in ACTIVE_WORDS:
            return True
    return False


def _status_pid(status: dict[str, Any] | None) -> int | None:
    if not status:
        return None
    for key in ("pid", "process_id", "runner_pid"):
        value = status.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_command_line(pid: int) -> str | None:
    """Return a command line for identity validation, never just a PID check."""
    try:
        if os.name != "nt":
            data = Path(f"/proc/{pid}/cmdline").read_bytes()
            return data.replace(b"\0", b" ").decode("utf-8", "replace")
        command = (
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = "
            + str(pid)
            + "'; if ($p) { [Console]::Out.Write($p.CommandLine) }"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout if result.returncode == 0 and result.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None


class ExecutionFileLock:
    """Small cross-platform advisory lock held while the direct child is alive."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class AgentState:
    def __init__(self, root: Path, token: str, validator: Path | None = None):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self.token = token
        self.validator = validator or Path(__file__).with_name("ws_validate.py")
        self.stop_file = self.root / "stop.request"
        self.mutex = threading.RLock()
        self.child: subprocess.Popen[bytes] | None = None
        self.child_command: list[str] | None = None
        self.child_subcommand: str | None = None
        self.exec_lock: ExecutionFileLock | None = None

    def status(self) -> dict[str, Any]:
        return _json_file(self.root / "status.json") or {
            "running": False,
            "state": "idle",
            "message": "status.json does not exist yet",
        }

    def _running_direct_child(self) -> bool:
        return self.child is not None and self.child.poll() is None

    def _restart_child(self) -> tuple[int, str] | None:
        status = self.status()
        if not _status_active(status):
            return None
        pid = _status_pid(status)
        command = str(status.get("subcommand") or status.get("command") or "")
        # RunLock is execution bookkeeping, not a competing status source.  It
        # supplies the PID only for an older runner status schema without one.
        if pid is None:
            lock = _json_file(self.root / "run.lock")
            pid = _status_pid(lock)
            if lock and not command:
                command = str(lock.get("command") or "")
        if pid is None or not _pid_exists(pid):
            return None
        return pid, command

    def _command_for(self, subcommand: str, args: dict[str, Any]) -> list[str]:
        if getattr(sys, "frozen", False):
            command = [sys.executable, "validate", subcommand]
        else:
            if not self.validator.is_file():
                raise RequestFailure(500, f"validator not found: {self.validator}")
            command = [sys.executable, str(self.validator), subcommand]
        command.extend(["--root", str(self.root), "--stop-file", str(self.stop_file)])
        for key, value in args.items():
            flag, validator = ARGUMENTS[key]
            if key == "design":
                checked = self._design(value)
            elif isinstance(validator, set):
                if not isinstance(value, str) or value not in validator:
                    choices = ", ".join(sorted(validator))
                    raise RequestFailure(400, f"{key} must be one of: {choices}")
                checked = value
            else:
                try:
                    checked = validator(value)
                except ValueError as exc:
                    raise RequestFailure(400, str(exc)) from exc
            command.extend([flag, str(checked)])
        return command

    def _design(self, value: Any) -> str:
        try:
            checked = _label(value, "design")
        except ValueError as exc:
            raise RequestFailure(400, str(exc)) from exc
        allowed: set[str] = set()
        config = _json_file(self.root / "config.json")
        custom = config.get("designs") if isinstance(config, dict) else None
        if isinstance(custom, dict):
            allowed.update(str(key) for key in custom if LABEL_RE.fullmatch(str(key)))
        if checked not in allowed:
            raise RequestFailure(400, "design is not provisioned in config.json")
        return checked

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"subcommand", "args"}:
            raise RequestFailure(400, "start body must contain exactly subcommand and args")
        subcommand, arguments = payload["subcommand"], payload["args"]
        if not isinstance(subcommand, str) or subcommand not in SUBCOMMANDS:
            raise RequestFailure(400, "unsupported validator subcommand")
        if not isinstance(arguments, dict):
            raise RequestFailure(400, "args must be a JSON object")
        unknown = sorted(set(arguments) - set(ARGUMENTS))
        if unknown:
            raise RequestFailure(400, f"unsupported args: {', '.join(unknown)}")
        command = self._command_for(subcommand, arguments)
        with self.mutex:
            if self._running_direct_child():
                raise RequestFailure(409, "a validator child is already running")
            inherited = self._restart_child()
            if inherited is not None:
                raise RequestFailure(409, f"status.json records active validator pid {inherited[0]}")
            execution_lock = ExecutionFileLock(self.root / ".ws-agent.lock")
            if not execution_lock.acquire():
                raise RequestFailure(409, "another agent owns the execution lock")
            with contextlib.suppress(FileNotFoundError):
                self.stop_file.unlink()
            kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            try:
                child = subprocess.Popen(command, **kwargs)
            except OSError as exc:
                execution_lock.release()
                raise RequestFailure(500, f"could not start validator: {exc}") from exc
            self.child = child
            self.child_command = command
            self.child_subcommand = subcommand
            self.exec_lock = execution_lock
            threading.Thread(target=self._wait_for_child, args=(child, execution_lock), daemon=True).start()
            # Avoid a start->watch race against an absent or previous status
            # document.  The runner is still the only writer; start merely
            # waits briefly until its PID is visible as the new truth.
            deadline = time.monotonic() + 2.0
            runner_status = None
            while time.monotonic() < deadline:
                candidate = _json_file(self.root / "status.json")
                if candidate and _status_pid(candidate) == child.pid and candidate.get("subcommand") == subcommand:
                    runner_status = candidate
                    break
                time.sleep(0.01)
            return {"accepted": True, "pid": child.pid, "subcommand": subcommand, "args": arguments,
                    "status_ready": runner_status is not None}

    def _wait_for_child(self, child: subprocess.Popen[bytes], execution_lock: ExecutionFileLock) -> None:
        child.wait()
        with self.mutex:
            if self.child is child:
                self.child = None
                self.child_command = None
                self.child_subcommand = None
                self.exec_lock = None
            execution_lock.release()

    def _matches_recorded_runner(self, pid: int, subcommand: str) -> bool:
        line = _pid_command_line(pid)
        if not line:
            return False
        folded = os.path.normcase(line)
        root_text = os.path.normcase(str(self.root))
        validator_name = "validate" if getattr(sys, "frozen", False) else self.validator.name
        return root_text in folded and validator_name.lower() in folded.lower() and (not subcommand or subcommand in line)

    def _kill_tree(self, pid: int) -> None:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode and _pid_exists(pid):
                raise RequestFailure(500, "taskkill could not terminate validator process tree")
        else:
            import signal

            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                return

    def stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"mode"} or payload.get("mode") not in {"graceful", "now"}:
            raise RequestFailure(400, "stop body must be {'mode':'graceful'|'now'}")
        mode = payload["mode"]
        with self.mutex:
            direct = self.child if self._running_direct_child() else None
            inherited = None if direct else self._restart_child()
            if direct is None and inherited is None:
                return {"accepted": False, "mode": mode, "message": "no active validator", "status": self.status()}
            # The runner recognizes the file contents and remains the sole
            # writer of status.json.  Even an immediate stop first gives it a
            # chance to kill workers and record stopped_at/exit_code itself.
            requested = "now" if mode == "now" else "graceful"
            temp = self.stop_file.with_name(self.stop_file.name + ".tmp")
            try:
                with temp.open("x", encoding="utf-8") as handle:
                    handle.write(requested + "\n")
                os.replace(temp, self.stop_file)
            except FileExistsError:
                # A previous atomic writer is in flight.  The existing stop
                # request is still valid; upgrade graceful to now below.
                with contextlib.suppress(FileNotFoundError):
                    temp.unlink()
                if mode == "now":
                    self.stop_file.write_text("now\n", encoding="utf-8")
            if mode == "now":
                pid = direct.pid if direct is not None else inherited[0]
                subcommand = self.child_subcommand or (inherited[1] if inherited else "")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    alive = direct.poll() is None if direct is not None else _pid_exists(pid)
                    if not alive or not _status_active(self.status()):
                        break
                    time.sleep(0.05)
                alive = direct.poll() is None if direct is not None else _pid_exists(pid)
                if alive:
                    if direct is None and not self._matches_recorded_runner(pid, subcommand):
                        raise RequestFailure(409, "recorded PID identity cannot be verified; use graceful stop")
                    self._kill_tree(pid)
            return {"accepted": True, "mode": mode, "status": self.status()}

    def nvidia_summary(self) -> dict[str, Any]:
        executable = shutil.which("nvidia-smi")
        if not executable:
            return {"available": False, "message": "nvidia-smi not found"}
        try:
            result = subprocess.run(
                [executable, "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "message": str(exc)}
        if result.returncode:
            return {"available": False, "message": result.stderr.strip() or "nvidia-smi failed"}
        rows = []
        for line in result.stdout.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) == 4:
                rows.append({"name": fields[0], "driver_version": fields[1], "memory_total_MB": fields[2], "memory_used_MB": fields[3]})
        return {"available": True, "gpus": rows}


class AgentHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ws-agent/1.1.0"
    timeout = 30

    @property
    def state(self) -> AgentState:
        return self.server.agent_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Ignore BaseHTTPRequestHandler's request-line argument because it
        # contains raw query values.  Record only the path and query key names.
        split = urlsplit(getattr(self, "path", ""))
        raw_keys = sorted(parse_qs(split.query, keep_blank_values=True))[:16]
        keys = [re.sub(r"[^A-Za-z0-9_.-]", "?", key)[:64] for key in raw_keys]
        target = (split.path or "/")[:256]
        if keys:
            target += "?keys=" + ",".join(keys)
        status = str(args[1]) if len(args) > 1 else "-"
        method = getattr(self, "command", "-")
        self.server.write_access_log(  # type: ignore[attr-defined]
            f'{self.client_address[0]} [{self.log_date_time_string()}] "{method} {target}" {status}'
        )

    def _send_json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str = "application/octet-stream") -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.state.token
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _require_auth(self) -> None:
        if not self._authorized():
            raise RequestFailure(401, "missing or invalid bearer token")

    def _body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestFailure(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestFailure(400, "invalid Content-Length") from exc
        if not 0 <= length <= MAX_BODY:
            raise RequestFailure(413, "request body is too large")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestFailure(400, "body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise RequestFailure(400, "body must be a JSON object")
        return value

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        try:
            self._require_auth()
            split = urlsplit(self.path)
            path = split.path
            if path == "/health":
                self._send_json(200, {"ok": True, "version": VERSION, "hostname": socket.gethostname(), "time": _utc_now()})
                return
            if path == "/env":
                self._send_json(200, {"env": _json_file(self.state.root / "env.json"), "nvidia_smi": self.state.nvidia_summary()})
                return
            if path == "/status":
                self._send_json(200, self.state.status())
                return
            if path == "/log":
                query = parse_qs(split.query, keep_blank_values=True)
                if set(query) - {"tail"} or len(query.get("tail", ["200"])) != 1:
                    raise RequestFailure(400, "log accepts only one tail parameter")
                try:
                    tail = int(query.get("tail", ["200"])[0])
                except ValueError as exc:
                    raise RequestFailure(400, "tail must be an integer") from exc
                if not 1 <= tail <= MAX_TAIL:
                    raise RequestFailure(400, f"tail must be between 1 and {MAX_TAIL}")
                log = self._latest_log()
                if log is None:
                    lines = []
                else:
                    with log.open("r", encoding="utf-8", errors="replace") as stream:
                        lines = list(deque((line.rstrip("\r\n") for line in stream), maxlen=tail))
                self._send_json(200, {"path": None if log is None else str(log.relative_to(self.state.root)), "lines": lines})
                return
            if path == "/receipts":
                self._send_json(200, {"receipts": self._receipt_list()})
                return
            if path.startswith("/receipt/"):
                name = unquote(path[len("/receipt/"):])
                if not RECEIPT_RE.fullmatch(name):
                    raise RequestFailure(403, "invalid receipt name")
                target = _safe_existing(self.state.root, f"receipts/{name}")
                self._send_file(target, "application/json; charset=utf-8")
                return
            if path.startswith("/artifact/"):
                target = _safe_existing(self.state.root, path[len("/artifact/"):], public_artifact=True)
                self._send_file(target)
                return
            raise RequestFailure(404, "unknown endpoint")
        except RequestFailure as exc:
            self._send_json(exc.status, {"error": exc.message, "status": exc.status})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # contain handler failures without exposing internals
            self._send_json(500, {"error": f"request failed: {type(exc).__name__}", "status": 500})

    def _latest_log(self) -> Path | None:
        runs = self.state.root / "runs"
        if not runs.is_dir() or _is_reparse(runs):
            return None
        candidates = []
        for target in runs.glob("*/log.txt"):
            try:
                safe = _safe_existing(self.state.root, target.relative_to(self.state.root).as_posix())
                candidates.append(safe)
            except RequestFailure:
                continue
        return max(candidates, key=lambda item: item.stat().st_mtime_ns, default=None)

    def _receipt_list(self) -> list[dict[str, Any]]:
        directory = self.state.root / "receipts"
        if not directory.is_dir() or _is_reparse(directory):
            return []
        answer = []
        for target in directory.iterdir():
            if not RECEIPT_RE.fullmatch(target.name):
                continue
            try:
                safe = _safe_existing(self.state.root, f"receipts/{target.name}")
            except RequestFailure:
                continue
            info = safe.stat()
            answer.append({"name": safe.name, "case_id": safe.stem, "size": info.st_size, "modified": dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc).isoformat().replace("+00:00", "Z")})
        return sorted(answer, key=lambda item: item["name"])

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        try:
            self._require_auth()
            split = urlsplit(self.path)
            if split.query:
                raise RequestFailure(400, "POST endpoints do not accept query parameters")
            body = self._body()
            if split.path == "/start":
                self._send_json(202, self.state.start(body))
                return
            if split.path == "/stop":
                self._send_json(200, self.state.stop(body))
                return
            raise RequestFailure(404, "unknown endpoint")
        except RequestFailure as exc:
            self._send_json(exc.status, {"error": exc.message, "status": exc.status})
        except Exception as exc:
            self._send_json(500, {"error": f"request failed: {type(exc).__name__}", "status": 500})

    def _unsupported(self) -> None:
        try:
            self._require_auth()
            raise RequestFailure(405, "method not allowed")
        except RequestFailure as exc:
            self._send_json(exc.status, {"error": exc.message, "status": exc.status})

    do_HEAD = _unsupported
    do_PUT = _unsupported
    do_DELETE = _unsupported
    do_PATCH = _unsupported
    do_OPTIONS = _unsupported


class _BoundedAccessLog:
    def __init__(self, root: Path, max_bytes: int, backups: int):
        self.directory = root / "agent-logs"
        self.path = self.directory / "agent.log"
        self.max_bytes = max_bytes
        self.backups = backups
        self.lock = threading.Lock()

    def write(self, message: str) -> None:
        data = (message.rstrip("\r\n") + "\n").encode("utf-8", "replace")
        if len(data) > self.max_bytes:
            data = data[:max(0, self.max_bytes - 1)] + b"\n"
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            current = self.path.stat().st_size if self.path.exists() else 0
            if current and current + len(data) > self.max_bytes:
                oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
                with contextlib.suppress(FileNotFoundError):
                    oldest.unlink()
                for index in range(self.backups - 1, 0, -1):
                    source = self.path.with_name(f"{self.path.name}.{index}")
                    if source.exists():
                        os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
                os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
            with self.path.open("ab") as handle:
                handle.write(data)


class AgentServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: AgentState, *,
                 max_active: int = DEFAULT_MAX_ACTIVE, request_timeout: float | None = None,
                 log_max_bytes: int = DEFAULT_LOG_MAX_BYTES,
                 log_backups: int = DEFAULT_LOG_BACKUPS):
        if max_active < 1:
            raise ValueError("max_active must be positive")
        if request_timeout is not None and request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if log_max_bytes < 1 or log_backups < 1:
            raise ValueError("log limits must be positive")
        handler = AgentHandler
        if request_timeout is not None:
            handler = type("ConfiguredAgentHandler", (AgentHandler,), {"timeout": request_timeout})
        self.max_active = max_active
        self._slots = threading.BoundedSemaphore(max_active)
        self._active = 0
        self._active_lock = threading.Lock()
        self._access_log = _BoundedAccessLog(state.root, log_max_bytes, log_backups)
        super().__init__(address, handler)
        self.agent_state = state

    @property
    def active_count(self) -> int:
        with self._active_lock:
            return self._active

    def write_access_log(self, message: str) -> None:
        self._access_log.write(message)

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        # Admission occurs before ThreadingMixIn creates a thread, so excess
        # half-open sockets cannot consume unbounded admission threads.
        if not self._slots.acquire(blocking=False):
            self._reject_saturated(request, client_address)
            return
        with self._active_lock:
            self._active += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_slot()
            raise

    def process_request_thread(self, request: socket.socket,
                               client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_slot()

    def _release_slot(self) -> None:
        with self._active_lock:
            self._active -= 1
        self._slots.release()

    def _reject_saturated(self, request: socket.socket,
                          client_address: tuple[str, int]) -> None:
        body = b'{"error":"server connection limit reached","status":503}'
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n" + body
        )
        try:
            # Read an already-available request header so Windows does not
            # convert close-with-unread-data into an RST that discards the 503.
            # A half-open peer is held for at most 50 ms and never gets a thread.
            request.settimeout(0.05)
            received = b""
            while len(received) < 8192 and b"\r\n\r\n" not in received:
                try:
                    chunk = request.recv(min(4096, 8192 - len(received)))
                except (TimeoutError, socket.timeout):
                    break
                if not chunk:
                    break
                received += chunk
            request.settimeout(0.25)
            request.sendall(response)
        except OSError:
            pass
        finally:
            self.write_access_log(f'{client_address[0]} [{_utc_now()}] "- saturated" 503')
            self.shutdown_request(request)

    def handle_error(self, request: socket.socket,
                     client_address: tuple[str, int]) -> None:
        error = sys.exc_info()[0]
        name = error.__name__ if error is not None else "unknown"
        self.write_access_log(f"{client_address[0]} [{_utc_now()}] handler_error={name}")


def serve(root: Path, bind: str, port: int, token_file: Path, validator: Path | None = None) -> None:
    state = AgentState(root, read_token(token_file), validator)
    with AgentServer((bind, port), state) as server:
        server.serve_forever(poll_interval=0.25)


def _self_check() -> dict[str, Any]:
    """Exercise the real HTTP and ws_ctl subprocess paths with a tiny validator."""
    ctl = Path(__file__).with_name("ws_ctl.py")
    if not ctl.is_file():
        raise RuntimeError(f"ws_ctl.py not found: {ctl}")
    with tempfile.TemporaryDirectory(prefix="ws-agent-check-") as raw:
        base = Path(raw)
        root = base / "root"
        root.mkdir()
        token_file = base / "token.txt"
        token_value = "correct-self-check-token-0123456789abcdef"
        token_file.write_text(token_value + "\n", encoding="utf-8")
        bad_token = base / "bad-token.txt"
        bad_token.write_text("wrong-self-check-token-0123456789abcdef\n", encoding="utf-8")

        class SelfCheckState(AgentState):
            def _command_for(self, subcommand: str, args: dict[str, Any]) -> list[str]:
                if getattr(sys, "frozen", False):
                    prefix = [sys.executable, "agent"]
                else:
                    prefix = [sys.executable, str(Path(__file__).resolve())]
                return [*prefix, "--self-check-worker", "--root", str(self.root),
                        "--stop-file", str(self.stop_file)]

        state = SelfCheckState(root, read_token(token_file))
        server = AgentServer(("127.0.0.1", 0), state)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"

        def ctl_call(*items: str, token: Path = token_file,
                     expect: int | tuple[int, ...] = 0) -> dict[str, Any]:
            prefix = [sys.executable, "ctl"] if getattr(sys, "frozen", False) else [sys.executable, str(ctl)]
            result = subprocess.run([*prefix, "--url", url, "--token-file", str(token), *items, "--json"], capture_output=True, text=True, timeout=15, check=False)
            expected = (expect,) if isinstance(expect, int) else expect
            if result.returncode not in expected:
                raise RuntimeError(f"ws_ctl {' '.join(items)} returned {result.returncode}: {result.stderr.strip()}")
            line = result.stdout.strip().splitlines()[-1]
            return json.loads(line)

        try:
            health = ctl_call("health")
            unauthorized = ctl_call("health", token=bad_token, expect=3)
            started = ctl_call("start", "env")
            conflict = ctl_call("start", "env", expect=5)
            watched = ctl_call("watch", "--interval", "0.05")
            receipts = ctl_call("receipts")
            # Start another real child so graceful stop is exercised, then observe stopped.
            ctl_call("start", "env")
            stopped = ctl_call("stop")
            stopped_watch = ctl_call("watch", "--interval", "0.05", "--until", "stopped", expect=10)
            from urllib.error import HTTPError
            from urllib.request import Request, urlopen
            req = Request(url + "/artifact/%2e%2e/config.json", headers={"Authorization": "Bearer " + token_value})
            try:
                urlopen(req, timeout=3)
            except HTTPError as exc:
                traversal = exc.code
            else:
                traversal = 200
            if traversal != 403:
                raise RuntimeError(f"path escape returned {traversal}, expected 403")
            result = {
                "ok": True,
                "checks": {
                    "health": health.get("ok") is True,
                    "unauthorized": unauthorized.get("status") == 401,
                    "child_pid": started.get("pid"),
                    "duplicate": conflict.get("status") == 409,
                    "watch": watched.get("running") is False and watched.get("exit_code") == 0,
                    "receipts": len(receipts.get("receipts", [])),
                    "stop": stopped.get("accepted") is True,
                    "stopped": stopped_watch.get("running") is False and stopped_watch.get("stopped_at") is not None,
                    "traversal": traversal,
                },
            }
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # Also exercise the actual env dispatcher.  A missing config/engine is
        # a valid diagnostic result (exit 1); the round trip must still create
        # runner-owned status.json and env.json without study data.
        real_root = base / "real-root"
        real_root.mkdir()
        real_state = AgentState(real_root, read_token(token_file))
        real_server = AgentServer(("127.0.0.1", 0), real_state)
        real_thread = threading.Thread(target=real_server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        real_thread.start()
        url = f"http://127.0.0.1:{real_server.server_address[1]}"
        try:
            real_start = ctl_call("start", "env")
            real_status = ctl_call("watch", "--interval", "0.05", expect=(0, 11))
            real_env = ctl_call("env")
            result["checks"]["real_env"] = bool(
                real_start.get("accepted") is True
                and real_status.get("subcommand") == "env"
                and real_status.get("running") is False
                and real_status.get("exit_code") in {0, 1}
                and isinstance(real_env.get("env"), dict)
            )
            result["ok"] = all(value is True or key in {"child_pid", "receipts", "traversal"}
                               for key, value in result["checks"].items())
        finally:
            real_server.shutdown()
            real_server.server_close()
            real_thread.join(timeout=2)
        return result


def _self_check_worker(root: Path, stop_file: Path) -> int:
    """Private local child used only by --self-check, including frozen builds."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "receipts").mkdir(parents=True, exist_ok=True)
    (root / "runs" / "selfcheck").mkdir(parents=True, exist_ok=True)

    def put(name: str, value: dict[str, Any]) -> None:
        target = root / name
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, target)

    base = {"schema_version": 1, "running": True, "subcommand": "env", "pid": os.getpid(),
            "started_at": _utc_now(), "updated_at": _utc_now(), "exit_code": None,
            "stopped_at": None}
    put("status.json", base)
    for _ in range(40):
        if stop_file.exists():
            put("status.json", {**base, "running": False, "updated_at": _utc_now(),
                                "exit_code": 130, "stopped_at": _utc_now(), "finished_at": _utc_now()})
            return 130
        time.sleep(0.05)
    put("env.json", {"self_check": True})
    (root / "receipts" / "self-check.json").write_text('{"ok":true}\n', encoding="utf-8")
    put("status.json", {**base, "running": False, "updated_at": _utc_now(),
                        "exit_code": 0, "finished_at": _utc_now()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="work directory owned by ws_validate.py")
    parser.add_argument("--bind", default="127.0.0.1", help="listen address (default: loopback)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--self-check", action="store_true", help="run a local, data-free HTTP round trip")
    parser.add_argument("--self-check-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--validator", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check_worker:
        if args.root is None or args.stop_file is None:
            build_parser().error("--self-check-worker requires --root and --stop-file")
        return _self_check_worker(args.root.resolve(), args.stop_file.resolve())
    if args.self_check:
        try:
            result = _self_check()
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, separators=(",", ":")))
            return 1
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    if args.root is None or args.token_file is None:
        build_parser().error("--root and --token-file are required unless --self-check is used")
    if not 0 <= args.port <= 65535:
        build_parser().error("--port must be between 0 and 65535")
    try:
        serve(args.root, args.bind, args.port, args.token_file, args.validator)
    except (OSError, ValueError) as exc:
        print(f"ws_agent: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
