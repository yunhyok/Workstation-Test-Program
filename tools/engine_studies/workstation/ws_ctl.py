"""Standard-library client for the workstation validation HTTP agent."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_FORBIDDEN = 4
EXIT_CONFLICT = 5
EXIT_NETWORK = 6
EXIT_REMOTE = 7
EXIT_STOPPED = 10
EXIT_FAILED = 11
EXIT_WATCH_NETWORK = 12


class ClientError(Exception):
    def __init__(self, message: str, exit_code: int, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.status = status
        self.payload = payload


def read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise ClientError(f"cannot read token file {path}: {exc}", EXIT_USAGE) from exc
    if not token or "\n" in token or "\r" in token:
        raise ClientError("token file must contain one non-empty token", EXIT_USAGE)
    return token


class Client:
    def __init__(self, base_url: str, token: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Fixed agent endpoints never redirect.  Refusing redirects also makes
        # it impossible to forward the Authorization header to another host.
        self.opener = build_opener(_NoRedirect())
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ClientError("--url must be an HTTP(S) origin without credentials", EXIT_USAGE)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ClientError("--url must not contain a path, query, or fragment", EXIT_USAGE)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, raw: bool = False) -> Any:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                parsed = {"error": exc.reason, "status": exc.code}
            message = parsed.get("error", str(exc)) if isinstance(parsed, dict) else str(exc)
            code = {401: EXIT_AUTH, 403: EXIT_FORBIDDEN, 409: EXIT_CONFLICT}.get(exc.code, EXIT_REMOTE)
            raise ClientError(f"HTTP {exc.code}: {message}", code, exc.code, parsed) from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ClientError(f"network error: {reason}", EXIT_NETWORK) from exc
        if raw:
            return payload
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ClientError("agent returned invalid JSON", EXIT_REMOTE) from exc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _parse_arg(value: str) -> tuple[str, Any]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--arg must be k=v")
    key, raw = value.split("=", 1)
    if key in {"jobs", "threads"}:
        try:
            return key, int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{key} must be an integer") from exc
    return key, raw


def _output(value: Any, machine: bool) -> None:
    if machine:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                rendered = json.dumps(item, ensure_ascii=False, indent=2)
            else:
                rendered = str(item)
            print(f"{key}: {rendered}")
    elif isinstance(value, list):
        for item in value:
            print(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else item)
    else:
        print(value)


def _terminal(status: dict[str, Any]) -> int | None:
    running = status.get("running") is True
    state = str(status.get("state", status.get("status", status.get("phase", "")))).lower()
    if running or state in {"running", "starting", "stopping", "active", "in_progress"}:
        return None
    if status.get("stopped_at") is not None or state in {"stopped", "cancelled", "canceled", "interrupted"}:
        return EXIT_STOPPED
    code = status.get("exit_code")
    if (isinstance(code, int) and code != 0) or state in {"failed", "error", "crashed"}:
        return EXIT_FAILED
    if (isinstance(code, int) and code == 0) or state in {"finished", "complete", "completed", "success", "succeeded"}:
        return 0
    return None


def _safe_relative(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ClientError("fetch path must be a safe relative POSIX path", EXIT_USAGE)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise ClientError("fetch path must not escape --out", EXIT_USAGE)
    return path


def _has_reparse(path: Path, root: Path) -> bool:
    import stat

    current = root
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    for part in parts:
        current = current / part
        if not current.exists():
            continue
        info = current.lstat()
        if current.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            return True
    return False


def _atomic_no_overwrite(data: bytes, out_dir: Path, relative: PurePosixPath) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve(strict=True)
    parent = root
    for part in relative.parts[:-1]:
        candidate = parent / part
        if candidate.exists():
            if _has_reparse(candidate, root) or not candidate.is_dir():
                raise ClientError("output path contains a symlink, reparse point, or file", EXIT_FORBIDDEN)
        else:
            try:
                candidate.mkdir()
            except FileExistsError:
                pass
            if _has_reparse(candidate, root) or not candidate.is_dir():
                raise ClientError("output path contains a symlink or reparse point", EXIT_FORBIDDEN)
        parent = candidate
    destination = parent / relative.name
    try:
        destination.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ClientError("output path escapes --out", EXIT_FORBIDDEN) from exc
    if destination.exists():
        raise ClientError(f"refusing to overwrite {destination}", EXIT_CONFLICT)
    fd, temp_name = tempfile.mkstemp(prefix=".ws-fetch-", dir=destination.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, destination)
        except FileExistsError as exc:
            raise ClientError(f"refusing to overwrite {destination}", EXIT_CONFLICT) from exc
        except OSError as exc:
            # Same-directory hard links should work on supported workstation filesystems.
            raise ClientError(f"cannot publish download atomically: {exc}", EXIT_REMOTE) from exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("health", "env", "status", "receipts"):
        sub.add_parser(name)
    log = sub.add_parser("log")
    log.add_argument("--tail", type=int, default=200)
    start = sub.add_parser("start")
    start.add_argument("subcommand", choices=["baseline", "matrix", "converge", "report", "gates", "env"])
    start.add_argument("--arg", action="append", default=[], type=_parse_arg, metavar="K=V")
    stop = sub.add_parser("stop")
    stop.add_argument("--now", action="store_true")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("name")
    fetch.add_argument("--out", required=True, type=Path)
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=float, default=30.0)
    watch.add_argument("--until", choices=["finished", "stopped"], default="finished")
    return parser


def _error_payload(exc: ClientError) -> dict[str, Any]:
    if isinstance(exc.payload, dict):
        return exc.payload
    answer: dict[str, Any] = {"error": str(exc)}
    if exc.status is not None:
        answer["status"] = exc.status
    return answer


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    # Machine mode is intentionally accepted at any position, including after a subcommand.
    machine = False
    while "--json" in values:
        values.remove("--json")
        machine = True
    args = build_parser().parse_args(values)
    if args.timeout <= 0:
        build_parser().error("--timeout must be positive")
    try:
        client = Client(args.url, read_token(args.token_file), args.timeout)
        command = args.command
        if command in {"health", "env", "status", "receipts"}:
            result = client.request("GET", "/" + command)
        elif command == "log":
            if not 1 <= args.tail <= 10_000:
                raise ClientError("--tail must be between 1 and 10000", EXIT_USAGE)
            result = client.request("GET", "/log?" + urlencode({"tail": args.tail}))
        elif command == "start":
            mapped: dict[str, Any] = {}
            for key, value in args.arg:
                if key in mapped:
                    raise ClientError(f"duplicate --arg key: {key}", EXIT_USAGE)
                mapped[key] = value
            result = client.request("POST", "/start", {"subcommand": args.subcommand, "args": mapped})
        elif command == "stop":
            result = client.request("POST", "/stop", {"mode": "now" if args.now else "graceful"})
        elif command == "fetch":
            relative = _safe_relative(args.name)
            if len(relative.parts) == 1 and relative.suffix.lower() == ".json" and relative.name not in {"summary.json"}:
                endpoint = "/receipt/" + quote(relative.name, safe="")
                output_relative = PurePosixPath(relative.name)
            else:
                endpoint = "/artifact/" + "/".join(quote(part, safe="") for part in relative.parts)
                output_relative = relative
            data = client.request("GET", endpoint, raw=True)
            destination = _atomic_no_overwrite(data, args.out, output_relative)
            result = {"ok": True, "path": str(destination), "bytes": len(data)}
        elif command == "watch":
            if args.interval <= 0:
                raise ClientError("--interval must be positive", EXIT_USAGE)
            previous: str | None = None
            while True:
                try:
                    status = client.request("GET", "/status")
                except ClientError as exc:
                    if machine:
                        event = "network_error" if exc.exit_code == EXIT_NETWORK else "remote_error"
                        _output({"event": event, "error": str(exc), "status": exc.status}, True)
                    else:
                        print(str(exc), file=sys.stderr)
                    return EXIT_WATCH_NETWORK if exc.exit_code == EXIT_NETWORK else exc.exit_code
                encoded = json.dumps(status, ensure_ascii=False, sort_keys=True)
                # JSON watch is NDJSON and reports every poll. Human mode suppresses repeats.
                if machine or encoded != previous:
                    _output(status, machine)
                previous = encoded
                terminal = _terminal(status)
                if terminal is not None:
                    return terminal
                time.sleep(args.interval)
        else:  # pragma: no cover - argparse guarantees this
            raise ClientError(f"unsupported command: {command}", EXIT_USAGE)
        _output(result, machine)
        return 0
    except ClientError as exc:
        if machine:
            _output(_error_payload(exc), True)
        else:
            print(f"ws_ctl: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
