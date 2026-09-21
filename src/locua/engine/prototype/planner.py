"""Offline ordinary-generation planner service, separate from RLCD selection.

A returned plan is a proposal. The caller must independently validate its task
contract, bind subjects to fresh observations, and authorize every effect.
"""
from __future__ import annotations
from ..runtime_paths import runtime_python, worker_environment

from copy import deepcopy
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time

from .decision import LAB, MemoryConfig, ModelService, ProtocolError

SERVICE_VERSION = "locua-planner-v3"
PLANNING_MODE = "ordinary_greedy_generation"
MAX_INPUT_TOKENS = 8192
MAX_OUTPUT_TOKENS = 1024
MAX_GENERATION_SECONDS = 60
MAX_JSON_BYTES = 256 * 1024


class PlannerError(RuntimeError):
    pass


def strict_json(raw):
    """One JSON value, no code fences, duplicate fields or nonfinite numbers."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError("Nonfinite JSON number: " + value)
    def floating(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Nonfinite JSON number: " + value)
        return parsed
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)


def _json_data(value):
    """Refuse coercions (tuple→list, nonstring keys→strings) at the boundary."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _json_data(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _json_data(child)
        return
    raise ValueError("Planner input must contain only finite JSON values and string keys")


def validate_inputs(request, scope, supplied_data=None):
    if not isinstance(request, str) or not request.strip():
        raise ValueError("Planner request must be nonempty text")
    if not isinstance(scope, dict):
        raise ValueError("Planner scope must be an explicit object")
    if supplied_data is not None and not isinstance(supplied_data, dict):
        raise ValueError("Supplied task data must be an object or null")
    try:
        _json_data(scope)
        _json_data(supplied_data)
        encoded = json.dumps({"request": request, "scope": scope, "supplied_data": supplied_data},
                             ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Planner input must be finite JSON data") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise ValueError("Planner request exceeds byte limit; no truncation")


def validate_limits(max_input_tokens, max_output_tokens, generation_timeout_s):
    if type(max_input_tokens) is not int or not 1 <= max_input_tokens <= MAX_INPUT_TOKENS:
        raise ValueError("Planner input-token limit must be 1..8192")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError("Planner output-token limit must be 1..1024")
    if type(generation_timeout_s) not in (int, float) or not 0 < generation_timeout_s <= MAX_GENERATION_SECONDS:
        raise ValueError("Planner generation deadline must be positive and at most 60 seconds")


def parse_plan_output(raw, finish_reason="stop"):
    """Strict syntax parsing only; semantic contract validation belongs outside."""
    try:
        if not isinstance(raw, str):
            raise ValueError("Planner output must be text")
        if len(raw.encode()) > MAX_JSON_BYTES:
            raise ValueError("Planner output exceeds byte limit")
        if finish_reason != "stop":
            raise ValueError("Generation did not finish normally: " + str(finish_reason))
        plan = strict_json(raw)
        if not isinstance(plan, dict):
            raise ValueError("Planner output must be one JSON object")
        return plan, None
    except (ValueError, TypeError, RecursionError) as exc:
        return None, {"type": type(exc).__name__, "message": str(exc)}


def model_pins():
    return json.loads((LAB / "probes/model_compare_v3_models.json").read_text())["models"]


class PlannerService(ModelService):
    """Resident planner process with the existing serial JSONL transport.

    A timeout poisons and terminates the channel. No automatic restart, repair,
    fallback or retry occurs. One plan request makes one generation call.
    """
    def __init__(self, model="baseline", *, max_input_tokens=MAX_INPUT_TOKENS,
                 max_output_tokens=MAX_OUTPUT_TOKENS, generation_timeout_s=MAX_GENERATION_SECONDS,
                 cache_limit_bytes=256 * 1024**2, memory_limit_bytes=10 * 1024**3,
                 startup_timeout_s=90, decoder="compact_greedy"):
        if model not in ("baseline", "comparator"):
            raise ValueError("Select the pinned baseline or explicit comparator")
        if decoder not in ("compact_greedy", "source_grammar"):
            raise ValueError("Select compact_greedy or the explicitly experimental source_grammar decoder")
        validate_limits(max_input_tokens, max_output_tokens, generation_timeout_s)
        if type(startup_timeout_s) not in (int, float) or not 0 < startup_timeout_s <= 300:
            raise ValueError("Planner startup deadline must be positive and <=300 seconds")
        if sys.platform != "darwin":
            raise RuntimeError("This pinned MLX planner currently supports macOS only; no fallback")
        self.memory_config = MemoryConfig(cache_limit_bytes, memory_limit_bytes)
        self.model_key, self.model_pin = model, model_pins()[model]
        self.decoder = decoder
        self.max_input_tokens, self.max_output_tokens = max_input_tokens, max_output_tokens
        self.generation_timeout_s = generation_timeout_s
        # Worker enforces the generation deadline, including a hard watchdog.
        # This allowance covers bounded JSON transport and cache cleanup only.
        self.timeout_s = generation_timeout_s + 5
        self.closed, self.poisoned = False, False
        self._lock, self._messages, self._seen_ids = threading.Lock(), queue.Queue(), set()
        command = ["/usr/bin/sandbox-exec", "-f", str(LAB / "probes/offline-macos.sb"),
                   runtime_python(), "-m", "locua.engine.prototype.planner_worker", "--model", model,
                   "--decoder", decoder,
                   "--max-input-tokens", str(max_input_tokens), "--max-output-tokens", str(max_output_tokens),
                   "--generation-timeout-s", str(generation_timeout_s),
                   "--cache-limit-bytes", str(cache_limit_bytes), "--memory-limit-bytes", str(memory_limit_bytes)]
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
        self.process = subprocess.Popen(command, cwd=LAB, env=worker_environment(env), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=sys.stderr)
        os.set_blocking(self.process.stdin.fileno(), False)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            ready = self._receive(time.monotonic() + startup_timeout_s)
            info = ready.get("info", {})
            if (not isinstance(info, dict) or ready.get("type") != "ready" or info.get("service") != SERVICE_VERSION
                    or info.get("model_key") != model or info.get("model_pin") != self.model_pin):
                raise ProtocolError("Worker did not establish requested planner/model identity")
            if info.get("decoder") != decoder:
                raise ProtocolError("Worker did not establish requested planner decoding mechanism")
            self.ready_info = info
        except BaseException:
            self.poisoned = True
            self.close()
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(2 * MAX_JSON_BYTES + 1)
                if not line:
                    raise EOFError("Local planner worker exited")
                if len(line) > 2 * MAX_JSON_BYTES or not line.endswith(b"\n"):
                    raise ProtocolError("Planner response exceeds line limit or is incomplete")
                value = strict_json(line)
                if not isinstance(value, dict):
                    raise ProtocolError("Planner response must be an object")
                self._messages.put(value)
        except BaseException as exc:
            self._messages.put(exc)

    def _receive(self, deadline):
        value = super()._receive(deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("Planner response arrived after its deadline")
        return value

    def plan(self, request, scope, supplied_data=None):
        validate_inputs(request, scope, supplied_data)
        started = time.perf_counter()
        rid, response = self._request("plan", {"request": request, "scope": deepcopy(scope),
                                              "supplied_data": deepcopy(supplied_data)})
        try:
            result = response["planning"]
            info = result["model_info"]
            if (info.get("service") != SERVICE_VERSION or info.get("model_key") != self.model_key
                    or info.get("model_pin") != self.model_pin or info.get("decoder") != self.decoder
                    or result.get("planning_mode") != PLANNING_MODE
                    or type(result.get("generation_calls")) is not int or result["generation_calls"] not in (0, 1)
                    or result.get("dispatched") is not False):
                raise ProtocolError("Planner identity, call count or no-dispatch contract violated")
            if result["generation_calls"] == 0 and result.get("finish_reason") not in ("timeout", "error"):
                raise ProtocolError("Successful planning requires exactly one generation call")
            usage = result["usage"]
            if (type(usage.get("input_tokens")) is not int or not 0 < usage["input_tokens"] <= self.max_input_tokens
                    or type(usage.get("output_tokens")) is not int or not 0 <= usage["output_tokens"] <= self.max_output_tokens):
                raise ProtocolError("Planner exceeded declared token limits")
            timing = result["timing"]
            if any(type(timing.get(key)) not in (int, float) or not math.isfinite(timing[key])
                   or timing[key] < 0 for key in ("generation_ms", "worker_total_ms")):
                raise ProtocolError("Planner timing must be finite nonnegative measurements")
            if result.get("finish_reason") == "stop" and timing["generation_ms"] > self.generation_timeout_s * 1000:
                raise ProtocolError("Planner completed after its declared generation deadline")
            plan, error = parse_plan_output(result["raw_output"], result["finish_reason"])
            from .intent_parser import VERSION as PROPOSAL_VERSION, compile_proposal
            proposal, provenance = plan, None
            if result.get("proposal_version") != PROPOSAL_VERSION:
                raise ProtocolError("Worker did not declare the compact proposal contract")
            if plan is not None:
                try:
                    grammar = result.get("grammar", {})
                    if self.decoder == "source_grammar":
                        if grammar.get("mechanism") != "field_local_token_trie" or grammar.get("complete") is not True:
                            raise ValueError("Constrained proposal did not complete its grammar")
                    elif grammar.get("mechanism") != "none":
                        raise ValueError("Unexpected generation constraint in compact_greedy mode")
                    plan, provenance = compile_proposal(proposal, request, scope, supplied_data)
                except (ValueError, TypeError, KeyError) as exc:
                    plan, error = None, {"type": type(exc).__name__, "message": str(exc)}
            if result["finish_reason"] == "timeout":
                # Preserve available partial output for audit, but never reuse a
                # generation channel after either cooperative or hard timeout.
                self.poisoned = True
                self.close()
            return {**result, "request_id": rid, "plan": plan, "parse_error": error,
                    "proposal": proposal, "compilation": provenance,
                    "contract_validated": False, "generation_was_not_rlcd": True,
                    "service_wall_ms": round((time.perf_counter() - started) * 1000, 3)}
        except (KeyError, TypeError, AttributeError, ProtocolError) as exc:
            self.poisoned = True
            self.close()
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError("Malformed planning response") from exc
