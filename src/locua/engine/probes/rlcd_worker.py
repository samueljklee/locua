#!/usr/bin/env python3
"""Resident local RLCD experiment worker; no desktop actions or cloud inference.

Run with .venv-rlcd/bin/python probes/rlcd_worker.py (Metal GPU access required).
Reads JSONL: {"id":"x","context":"...","schema":{"action":{"type":"enum",
"description":"Choose the next action label","choices":["A","B","C"]}}}.
Writes a ready record, then {id, ok, result, wall_ms}. The exact upstream result
is retained; its 'prob' fields are explicitly NOT calibrated confidence.

This restricted adapter calls the unchanged upstream inference function. It
rejects multi-field, colliding, multi-token, or tokenizer-boundary-unsafe labels.
Use Runtime.decide(context, schema_dict) for in-process integration.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

LAB = Path(__file__).resolve().parents[1]
from locua.engine.runtime_paths import model_cache, verify_source, verify_model
UPSTREAM = LAB / "vendor/qwen"
PIN = json.loads((LAB / "probes/rlcd-model.json").read_text())
os.environ["HF_HOME"] = str(model_cache())
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, str(UPSTREAM))


class Runtime:
    def __init__(self):
        started = time.perf_counter()
        verify_source()
        snapshot = verify_model("baseline")
        # Fail explicitly if GPU access is unavailable; never let router fallback.
        import mlx.core as mx
        from core import engine_mlx
        from core.schema import StructuredSchema
        if engine_mlx.MODEL_ID != PIN["model_id"]:
            raise RuntimeError("Upstream model identity changed")
        original_loader = engine_mlx.load
        def pinned_local_load(requested_id):
            if requested_id != PIN["model_id"]:
                raise RuntimeError("Unexpected model request; fallback is forbidden")
            return original_loader(str(snapshot))
        engine_mlx.load = pinned_local_load
        try:
            with contextlib.redirect_stdout(sys.stderr):
                self.model, self.tokenizer = engine_mlx.get_engine()
        finally:
            engine_mlx.load = original_loader
        self.engine = engine_mlx
        self.Schema = StructuredSchema
        self.mx = mx
        self.load_ms = (time.perf_counter() - started) * 1000
        self.identity = (id(self.model), id(self.tokenizer))
        self.calls = 0

    def info(self):
        return {**PIN, "backend": "mlx", "mlx_version": self.mx.__version__,
                "load_and_warmup_ms": round(self.load_ms, 3), "calls": self.calls,
                "offline_libraries": True, "adapter": "restricted_exact_upstream",
                "confidence_is_calibrated": False}

    def prepare(self, schema_dict):
        if not isinstance(schema_dict, dict) or len(schema_dict) != 1:
            raise ValueError("Pilot requires exactly one enum decision field")
        name, spec = next(iter(schema_dict.items()))
        choices = spec.get("choices")
        if spec.get("type", "enum") != "enum" or not isinstance(choices, list):
            raise ValueError("Pilot requires enum choices")
        if not 2 <= len(choices) <= 255 or any(not isinstance(x, str) for x in choices):
            raise ValueError("Require 2–255 string labels")
        if len(set(choices)) != len(choices):
            raise ValueError("Duplicate labels")
        # JSON copy keeps caller mutation from changing an already compiled schema.
        schema = self.Schema(json.loads(json.dumps(schema_dict)))
        meta = schema.compile_parallel_metadata(self.tokenizer)
        prefix = meta["prefixes"][0]
        if prefix or meta["has_collisions"][0]:
            raise ValueError("Pilot labels must have no common prefix or scored-token collision")
        suffix = f'  "{name}": "'
        suffix_tokens = self.tokenizer.encode(suffix, add_special_tokens=False)
        for label, candidate_token in zip(choices, meta["cands_per_field"][0]):
            tokens = self.tokenizer.encode(label, add_special_tokens=False)
            if len(tokens) != 1 or tokens[0] != candidate_token:
                raise ValueError(f"Not a single decision token: {label!r}")
            if self.tokenizer.decode(tokens) != label:
                raise ValueError(f"Label does not round trip: {label!r}")
            combined = self.tokenizer.encode(suffix + label, add_special_tokens=False)
            if combined != suffix_tokens + tokens:
                raise ValueError(f"Label changes suffix tokenization: {label!r}")
        return schema

    def decide(self, context, schema_dict):
        if not isinstance(context, str):
            raise ValueError("Context must be text")
        if len(self.tokenizer.encode(context)) > 8192:
            raise ValueError("Pilot context exceeds 8192 tokens")
        schema = self.prepare(schema_dict)
        started = time.perf_counter()
        result = self.engine.run_parallel_generation(context, schema)
        wall_ms = (time.perf_counter() - started) * 1000
        if (id(self.engine.get_engine()[0]), id(self.engine.get_engine()[1])) != self.identity:
            raise RuntimeError("Model instance changed unexpectedly")
        self.calls += 1
        return {"result": result, "wall_ms": round(wall_ms, 3),
                "call_number": self.calls, "confidence_is_calibrated": False}


def main():
    with contextlib.redirect_stdout(sys.stderr):
        runtime = Runtime()
    print(json.dumps({"type": "ready", **runtime.info()}), flush=True)
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if request.get("op") == "info":
                response = {"id": request.get("id"), "ok": True, "info": runtime.info()}
            else:
                with contextlib.redirect_stdout(sys.stderr):
                    decision = runtime.decide(request["context"], request["schema"])
                response = {"id": request.get("id"), "ok": True, **decision}
        except Exception as exc:
            response = {"id": request.get("id"), "ok": False,
                        "error": {"type": type(exc).__name__, "message": str(exc)}}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
