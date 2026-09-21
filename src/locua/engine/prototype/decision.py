"""Local resident decision-service boundary. This module never dispatches GUI IO.

Cancellation is the caller's responsibility: after choose returns, check the
current cancellation/scope state before dispatch. A timed-out channel is poisoned
and terminated; no late reply can be reused for a later request.
"""
from __future__ import annotations
from ..runtime_paths import runtime_python, worker_environment

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import select
import subprocess
import sys
import threading
import time
import uuid

LAB = Path(__file__).resolve().parents[1]
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ACTIONS = 254  # Existing exact worker supports <=255 labels; reserve abstain.


class DecisionError(RuntimeError):
    pass


class ProtocolError(DecisionError):
    pass


@dataclass(frozen=True)
class MemoryConfig:
    cache_limit_bytes: int = 256 * 1024**2
    memory_limit_bytes: int = 10 * 1024**3

    def __post_init__(self):
        if type(self.cache_limit_bytes) is not int or type(self.memory_limit_bytes) is not int:
            raise ValueError("Memory limits must be integer byte counts")
        if self.memory_limit_bytes <= 0 or not 0 <= self.cache_limit_bytes <= self.memory_limit_bytes:
            raise ValueError("Require 0 <= cache limit <= positive memory guideline")


def validate_request(goal, observation_summary, candidates, history, max_context_tokens=8192):
    if type(max_context_tokens) is not int or not 256 <= max_context_tokens <= 8192:
        raise ValueError("Prototype context limit must be 256..8192 tokens")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("An explicit nonempty user goal is required")
    if not isinstance(observation_summary, str) or not observation_summary.strip():
        raise ValueError("An explicit observed-state summary is required")
    if not isinstance(candidates, list) or len(candidates) > MAX_ACTIONS:
        raise ValueError(f"Support at most {MAX_ACTIONS} action candidates; none may be dropped")
    ids = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"id", "description"}:
            raise ValueError("Each candidate must contain exactly id and description; keep tool mapping outside model")
        if any(not isinstance(candidate[k], str) or not candidate[k].strip() for k in ("id", "description")):
            raise ValueError("Candidate IDs and descriptions must be nonempty strings")
        ids.append(candidate["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate candidate IDs")
    if not isinstance(history, list) or len(history) > 64:
        raise ValueError("History must be an explicit list of at most 64 entries; no automatic truncation")
    if any(not isinstance(item, dict) or set(item) != {"action", "outcome"}
           or any(not isinstance(v, str) for v in item.values()) for item in history):
        raise ValueError("History entries require action/outcome strings")
    encoded = json.dumps({"goal": goal, "observation_summary": observation_summary,
                          "candidates": candidates, "history": history}, ensure_ascii=False).encode()
    if len(encoded) > MAX_REQUEST_BYTES - 2048:
        raise ValueError("Request exceeds byte limit; no automatic truncation")


class ModelService:
    def __init__(self, model="baseline", *, cache_limit_bytes=256 * 1024**2,
                 memory_limit_bytes=10 * 1024**3, max_context_tokens=8192,
                 timeout_s=60, startup_timeout_s=90):
        if model not in ("baseline", "comparator"):
            raise ValueError("Select baseline or the explicitly experimental comparator")
        self.memory_config = MemoryConfig(cache_limit_bytes, memory_limit_bytes)
        if type(max_context_tokens) is not int or not 256 <= max_context_tokens <= 8192:
            raise ValueError("Prototype context limit must be 256..8192 tokens")
        if not 0 < timeout_s <= 300 or not 0 < startup_timeout_s <= 300:
            raise ValueError("Process deadlines must be positive and <=300 seconds")
        if sys.platform != "darwin":
            raise RuntimeError("This pinned MLX prototype has only a macOS backend; no fallback")
        self.max_context_tokens = max_context_tokens
        self.model_pin = json.loads((LAB / "probes/model_compare_v3_models.json").read_text())["models"][model]
        self.timeout_s = timeout_s
        self.closed, self.poisoned = False, False
        self._lock = threading.Lock()
        self._messages = queue.Queue()
        self._seen_ids = set()
        command = ["/usr/bin/sandbox-exec", "-f", str(LAB / "probes/offline-macos.sb"),
            runtime_python(), "-m", "locua.engine.prototype.model_worker", "--model", model,
            "--cache-limit-bytes", str(cache_limit_bytes), "--memory-limit-bytes", str(memory_limit_bytes),
            "--max-context-tokens", str(max_context_tokens)]
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
        self.process = subprocess.Popen(command, cwd=LAB, env=worker_environment(env), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=sys.stderr)
        os.set_blocking(self.process.stdin.fileno(), False)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            ready = self._receive(time.monotonic() + startup_timeout_s)
            if (ready.get("type") != "ready" or ready.get("info", {}).get("model_key") != model
                    or ready.get("info", {}).get("model_pin") != self.model_pin):
                raise ProtocolError("Worker did not establish the requested model identity: " + repr(ready))
            self.ready_info = ready["info"]
        except BaseException:
            self.poisoned = True
            self.close()
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_RESPONSE_BYTES + 1)
                if not line:
                    raise EOFError("Local decision worker exited")
                if len(line) > MAX_RESPONSE_BYTES or not line.endswith(b"\n"):
                    raise ProtocolError("Worker response exceeds line limit or is incomplete")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ProtocolError("Worker response is not an object")
                self._messages.put(value)
        except BaseException as exc:
            self._messages.put(exc)

    def _receive(self, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError("Local decision deadline exceeded; response must not be dispatched")
        try:
            value = self._messages.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise TimeoutError("Local decision deadline exceeded; response must not be dispatched") from exc
        if time.monotonic() >= deadline:
            raise TimeoutError("Local decision deadline exceeded; response must not be dispatched")
        if isinstance(value, BaseException):
            raise value
        return value

    def _send(self, payload, deadline):
        value = memoryview((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        if len(value) > MAX_REQUEST_BYTES:
            raise ValueError("Worker request exceeds byte limit")
        fd = self.process.stdin.fileno()
        while value:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise TimeoutError("Local worker write deadline exceeded")
            try:
                size = os.write(fd, value)
            except BlockingIOError:
                continue
            if size <= 0:
                raise BrokenPipeError("Local worker stdin closed")
            value = value[size:]

    def _request(self, op, payload=None, request_id=None):
        with self._lock:
            if self.closed or self.poisoned:
                raise ProtocolError("Decision service is closed/poisoned; create a new service")
            rid = request_id or str(uuid.uuid4())
            if not isinstance(rid, str) or not rid or rid in self._seen_ids:
                raise ValueError("Request IDs must be nonempty and unique within the service")
            self._seen_ids.add(rid)
            deadline = time.monotonic() + self.timeout_s
            try:
                self._send({"id": rid, "op": op, **(payload or {})}, deadline)
                response = self._receive(deadline)
                if response.get("id") != rid or type(response.get("ok")) is not bool:
                    raise ProtocolError("Unmatched/malformed worker response; channel cannot be reused")
            except BaseException:
                self.poisoned = True
                self.close()
                raise
            if not response["ok"]:
                raise DecisionError(json.dumps(response.get("error", {})))
            return rid, response

    def info(self):
        return self._request("info")[1]["info"]

    def choose(self, goal, observation_summary, candidates, history=None, *, request_id=None):
        history = [] if history is None else history
        validate_request(goal, observation_summary, candidates, history, self.max_context_tokens)
        rid, response = self._request("choose", {"goal": goal, "observation_summary": observation_summary,
            "candidates": candidates, "history": history}, request_id)
        try:
            decision = response["decision"]
            selected = decision["selected_id"]
            ids = [c["id"] for c in candidates]
            if selected is not None and selected not in ids:
                raise ProtocolError("Worker selected an unknown candidate")
            if decision.get("abstained") is not (selected is None):
                raise ProtocolError("Worker abstention/selection are inconsistent")
            if decision["coverage"]["candidate_ids"] != ids or decision["coverage"]["omitted_candidates"] != 0:
                raise ProtocolError("Worker did not cover the exact entire candidate list")
            if decision.get("dispatched") is not False:
                raise ProtocolError("Decision service must never claim desktop dispatch")
            return {"request_id": rid, **decision}
        except (KeyError, TypeError, ProtocolError):
            self.poisoned = True
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self._reader.join(timeout=1)
        if self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
