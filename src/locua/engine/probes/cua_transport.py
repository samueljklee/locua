#!/usr/bin/env python3
"""Experimental persistent MCP transport to an already-running Cua daemon.

No permission requests, daemon startup, app launch, or retries are implicit.
A timeout poisons the connection: an action might have completed, so its effect
must be reconciled through a new connection before any retry is considered.
Raw responses are preserved; transport success is not task success.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import select
import subprocess
import sys
import threading
import time


class CuaTransport:
    def __init__(self, socket_path=None, timeout=45, binary_path=None):
        socket_path = Path(socket_path or Path.home() / "Library/Caches/cua-driver/cua-driver.sock")
        if not socket_path.is_socket():
            raise RuntimeError(f"Authorized Cua daemon is not listening at {socket_path}; no auto-start")
        self.timeout = timeout
        self.next_id = 0
        self.poisoned = False
        self.lock = threading.Lock()
        self.messages = queue.Queue()
        env = dict(os.environ, CUA_DRIVER_RS_TELEMETRY_ENABLED="false")
        self.process = subprocess.Popen(
            [str(binary_path or Path.home() / ".local/bin/cua-driver"), "mcp", "--socket", str(socket_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
            text=True, bufsize=1, env=env)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.server = self.rpc("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "locua-lab", "version": "0.0.1"}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.inventory = self.rpc("tools/list", {})
            self.tool_names = {tool["name"] for tool in self.inventory["tools"]}
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except BaseException as error:
            self.messages.put(error)
        finally:
            self.messages.put(EOFError("Cua MCP stdout closed"))

    def _send(self, value, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout
        pending = memoryview((json.dumps(value, ensure_ascii=False) + "\n").encode())
        fd = self.process.stdin.fileno()
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise TimeoutError("MCP write deadline exceeded; dispatch effect is unknown")
            try:
                count = os.write(fd, pending)
            except BlockingIOError:
                continue
            if count <= 0:
                raise BrokenPipeError("MCP write returned no bytes")
            pending = pending[count:]

    def rpc(self, method, params):
        with self.lock:
            if self.poisoned:
                raise RuntimeError("Connection is indeterminate; dispatch disabled")
            self.next_id += 1
            request_id = self.next_id
            deadline = time.monotonic() + self.timeout
            try:
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, deadline)
                while True:
                    try:
                        message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty as error:
                        raise TimeoutError("MCP response deadline exceeded; dispatch effect is unknown") from error
                    if isinstance(message, BaseException):
                        raise message
                    if "method" in message:
                        if "id" in message:
                            reply = {"jsonrpc": "2.0", "id": message["id"]}
                            if message["method"] == "ping":
                                reply["result"] = {}
                            else:
                                reply["error"] = {"code": -32601,
                                                  "message": "Client method not supported"}
                            self._send(reply, deadline)
                        continue
                    if "id" not in message:
                        continue
                    if message["id"] != request_id:
                        raise RuntimeError("Unexpected response id; connection state is indeterminate")
                    if "error" in message:
                        raise RuntimeError(json.dumps(message["error"]))
                    return message["result"]
            except BaseException:
                self.poisoned = True
                raise

    def call(self, name, arguments):
        if name not in self.tool_names:
            raise ValueError(f"Tool absent from runtime inventory: {name}")
        started = time.perf_counter()
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return {"result": result, "wall_ms": round((time.perf_counter() - started) * 1000, 3)}

    def close(self):
        # Closing the proxy does not undo in-flight native effects or grant
        # cancellation guarantees. Explicit end_session belongs to the caller.
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=1)
        if self.process.stdout and not self.process.stdout.closed:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket")
    parser.add_argument("--inventory-out", type=Path)
    parser.add_argument("--trace", type=Path, help="New local JSONL trace; may contain screen data")
    parser.add_argument("--compact-output", action="store_true", help="Print status only; preserve full responses in --trace")
    args = parser.parse_args()
    if args.compact_output and not args.trace:
        parser.error("--compact-output requires --trace")
    trace = None
    if args.trace:
        args.trace.parent.mkdir(parents=True, exist_ok=True)
        trace = os.fdopen(os.open(args.trace, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
    with CuaTransport(args.socket) as driver:
        if args.inventory_out:
            args.inventory_out.parent.mkdir(parents=True, exist_ok=True)
            args.inventory_out.write_text(json.dumps(driver.inventory, indent=2) + "\n")
        ready = {"type": "ready", "server": driver.server, "tool_count": len(driver.tool_names)}
        if trace:
            trace.write(json.dumps(ready) + "\n")
            trace.flush()
        print(json.dumps(ready), flush=True)
        for line in sys.stdin:
            request = {}
            started_at = datetime.now(timezone.utc).isoformat()
            try:
                request = json.loads(line)
                result = driver.call(request["name"], request.get("arguments", {}))
                response = {"id": request.get("id"), "ok": True, **result}
            except Exception as error:
                response = {"id": request.get("id"), "ok": False,
                            "error": {"type": type(error).__name__, "message": str(error)}}
            if trace:
                trace.write(json.dumps({"started_at_utc": started_at,
                                        "ended_at_utc": datetime.now(timezone.utc).isoformat(),
                                        "request": request, "response": response}, ensure_ascii=False) + "\n")
                trace.flush()
            display = response
            if args.compact_output:
                display = {key: value for key, value in response.items() if key != "result"}
                result = response.get("result", {})
                display["isError"] = result.get("isError", False)
                display["status"] = {key: value for key, value in result.get("structuredContent", {}).items()
                                     if key in ("effect", "status", "code", "refusal", "snapshot_id", "revived")}
            print(json.dumps(display, ensure_ascii=False), flush=True)
            if driver.poisoned:
                break
    if trace:
        trace.close()


if __name__ == "__main__":
    main()
