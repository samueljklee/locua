#!/usr/bin/env python3
"""Paired local Qwen size comparison; frozen policy, unchanged RLCD math, no GUI.

One model per process. Baseline is the existing exact worker. Comparator changes
only model selection/loading in memory; reference checkout and frozen files stay
untouched. --smoke uses declared synthetic architecture checks, never fresh gold.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from pathlib import Path
import resource
import subprocess
import sys
import time

LAB = Path(__file__).resolve().parents[1]
CONFIG_PATH = LAB / "probes/model_compare_v3_models.json"
CONFIG = json.loads(CONFIG_PATH.read_text())
VERSION = "qwen25-size-rlcd-v3.0"

# Importing the frozen worker sets offline library flags but does not load MLX.
from rlcd_worker import Runtime as ExactRuntime, UPSTREAM
from locua.engine.runtime_paths import model_cache, verify_source, verify_model
from compact_policy_v2 import compile_case, score_case, policy_fingerprint, LABELS


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprints():
    paths = ["probes/model_compare_v3.py", "probes/model_compare_v3_models.json",
             "probes/compact_policy_v2.py", "probes/rlcd_worker.py",
             "probes/rlcd-model.json", "probes/requirements-rlcd.lock.txt",
             "results/model-compare-v3-provision.json", "probes/model_compare_v3_test.py"]
    return {p: digest(LAB / p) for p in paths}


def guard_policy():
    if policy_fingerprint() != CONFIG["policy_sha256"]:
        raise RuntimeError("Frozen compact policy changed")
    baseline = json.loads((LAB / "probes/rlcd-model.json").read_text())
    expected = CONFIG["models"]["baseline"]
    if any(baseline[k] != expected[k] for k in ("model_id", "revision")):
        raise RuntimeError("Frozen baseline identity changed")
    verify_source()


class ComparatorRuntime(ExactRuntime):
    """Reuse EXACT prepare/decide; bind a single pinned comparator at load time."""

    def __init__(self):
        started = time.perf_counter()
        guard_policy()
        self.pin = CONFIG["models"]["comparator"]
        provision = json.loads((LAB / "probes/comparator-files.json").read_text())
        if provision["model"] != self.pin:
            raise RuntimeError("Comparator provisioning record differs from config")
        snapshot = verify_model("comparator")
        self.integrity_check_ms = (time.perf_counter() - started) * 1000
        self.loaded_files = provision["files"]
        self.snapshot_relative = str(snapshot)
        import mlx.core as mx
        from core import engine_mlx
        from core.schema import StructuredSchema
        if engine_mlx._model is not None or engine_mlx._tokenizer is not None:
            raise RuntimeError("Use a fresh process for each model")
        original_loader = engine_mlx.load
        original_id = engine_mlx.MODEL_ID
        if original_id != CONFIG["models"]["baseline"]["model_id"]:
            raise RuntimeError("Unexpected original upstream model identity")

        def pinned_local_load(requested_id):
            if requested_id != self.pin["model_id"]:
                raise RuntimeError("Unexpected model request; fallback is forbidden")
            return original_loader(str(snapshot))

        # Original get_engine (including its warmup), schema and inference
        # function are left intact. Only selection/loading globals are bound.
        engine_mlx.MODEL_ID = self.pin["model_id"]
        engine_mlx.load = pinned_local_load
        try:
            with contextlib.redirect_stdout(sys.stderr):
                self.model, self.tokenizer = engine_mlx.get_engine()
        finally:
            engine_mlx.load = original_loader
        self.engine, self.Schema, self.mx = engine_mlx, StructuredSchema, mx
        self.load_ms = (time.perf_counter() - started) * 1000
        self.identity = (id(self.model), id(self.tokenizer))
        self.calls = 0

    def info(self):
        return {**self.pin, "upstream_revision": CONFIG["upstream_revision"],
            "backend": "mlx", "mlx_version": self.mx.__version__,
            "load_and_warmup_ms": round(self.load_ms, 3),
            "file_integrity_check_ms": round(self.integrity_check_ms, 3),
            "calls": self.calls, "offline_libraries": True,
            "confidence_is_calibrated": False,
            "prepare_and_decide_inherited_unchanged": True,
            "loaded_snapshot": self.snapshot_relative,
            "verified_loaded_files": self.loaded_files,
            "source_checkout_modified": False}


def memory(runtime):
    return {"mlx_active_bytes": runtime.mx.get_active_memory(),
            "mlx_peak_bytes": runtime.mx.get_peak_memory(),
            "mlx_cache_bytes": runtime.mx.get_cache_memory(),
            "process_maxrss_bytes_macos": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "not_total_system_memory": True}


def runtime_info(runtime):
    result = runtime.info()
    fields = ("model_type", "hidden_size", "intermediate_size", "num_hidden_layers",
              "num_attention_heads", "num_key_value_heads", "vocab_size")
    result["loaded_model_args"] = {k: getattr(runtime.model.args, k, None) for k in fields}
    result["loaded_python_model_type"] = type(runtime.model).__module__ + "." + type(runtime.model).__name__
    result["loaded_layer_count"] = len(runtime.model.layers)
    return result


def installed_dependencies():
    versions = {}
    for line in (LAB / "probes/requirements-rlcd.lock.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, pinned = line.split("==", 1)
        actual = metadata.version(name)
        if actual != pinned:
            raise RuntimeError(f"Installed dependency differs from lock: {name}")
        versions[name] = actual
    return versions


def validate_freeze(frozen, cases_path, record):
    expected = frozen["model_comparison"]
    if frozen.get("schema_version") != "locua-model-compare-v3-freeze/1":
        raise ValueError("Unsupported independent freeze schema")
    checks = {frozen["policy_path"]: frozen["policy_sha256"],
              expected["worker_path"]: expected["worker_sha256"],
              expected["config_path"]: expected["config_sha256"],
              **expected["dependency_hashes"]}
    for path, sha in record["fingerprints"].items():
        if checks.get(path) != sha:
            raise ValueError(f"Source/config/dependency missing or changed in freeze: {path}")
    for path, sha in checks.items():
        if digest(LAB / path) != sha:
            raise ValueError(f"Frozen dependency changed: {path}")
    if digest(cases_path) != frozen["scored_inputs_sha256"]:
        raise ValueError("Scored bundle does not match independent freeze")
    for key in ("baseline", "comparator"):
        model = expected["models"][key]
        if any(model[x] != CONFIG["models"][key][x] for x in ("model_id", "revision")):
            raise ValueError("Frozen model identity differs")
        if model["upstream_revision"] != CONFIG["upstream_revision"] or model["backend"] != "mlx":
            raise ValueError("Frozen model mechanism differs")


def smoke(runtime):
    trials = []
    for size, expected in ((2, "B"), (4, "C"), (32, "f")):
        choices = LABELS[:size]
        schema = {"action": {"type": "enum", "description": "The explicitly requested option label", "choices": choices}}
        context = f"Synthetic runtime connectivity check. The user explicitly requests option {expected}. Select {expected}."
        # prepare enforces actual tokenizer single-token, boundary and collision
        # requirements before exact upstream inference for every label set.
        decision = runtime.decide(context, schema)
        chosen = decision["result"]["parsed_json"]["action"]["value"]
        trials.append({"labels": choices, "expected": expected, "selected": chosen,
            "correct": chosen == expected, "raw": decision,
            "actual_label_tokens": {x: runtime.tokenizer.encode(x, add_special_tokens=False) for x in choices}})
    return {"purpose": "synthetic architecture/tokenizer/connectivity only; no computer-use quality claim", "trials": trials}


def validate_cases(data):
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise ValueError("Expected nonempty model-facing cases list")
    ids = [x.get("id") for x in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate case IDs")
    for case in cases:
        if case.get("evaluation", "score") != "score":
            raise ValueError("Scored input bundle contains coverage-only case")
        if case.get("observation", {}).get("kind") not in ("browser_semantic_v2", "native_window_state"):
            raise ValueError("Fresh comparison requires recorded observations")
        # Compile all orders before loading; overflow is a coverage exclusion,
        # not an inference failure to suppress after seeing model choices.
        for order in range(3):
            compile_case(case, order)
    return cases


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", choices=("baseline", "comparator"), required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--cases", type=Path)
    p.add_argument("--freeze", type=Path)
    p.add_argument("--freeze-sha256")
    args = p.parse_args()
    if args.output.exists():
        raise ValueError("Refusing to overwrite comparison evidence")
    guard_policy()
    record = {"schema_version": "locua-model-compare-v3-results/1", "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(), "model_key": args.model,
        "model_pin": CONFIG["models"][args.model], "fingerprints": fingerprints(),
        "worker_sha256": digest(Path(__file__)), "config_sha256": digest(CONFIG_PATH),
        "policy_sha256": policy_fingerprint(), "orders": 3, "dispatched": False,
        "confidence_is_calibrated": False,
        "installed_dependencies": installed_dependencies(), "python_version": sys.version}
    cases = None
    if not args.smoke:
        if not all((args.cases, args.freeze, args.freeze_sha256)):
            raise ValueError("Fresh scoring requires cases and independent corpus freeze digest")
        if digest(args.freeze) != args.freeze_sha256:
            raise ValueError("Corpus freeze digest mismatch")
        frozen = json.loads(args.freeze.read_text())
        validate_freeze(frozen, args.cases, record)
        cases = validate_cases(json.loads(args.cases.read_text()))
        record.update({"freeze_sha256": args.freeze_sha256,
            "cases_file_sha256": digest(args.cases), "case_ids": [x["id"] for x in cases]})
    with contextlib.redirect_stdout(sys.stderr):
        runtime = ExactRuntime() if args.model == "baseline" else ComparatorRuntime()
    record["runtime_initial"] = runtime_info(runtime)
    record["memory_after_load"] = memory(runtime)
    if args.smoke:
        record["smoke"] = smoke(runtime)
    else:
        record["trials"] = [score_case(runtime, case, order) for case in cases for order in range(3)]
    record["runtime"] = runtime_info(runtime)
    record["memory_after_trials"] = memory(runtime)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(json.dumps({"output": str(args.output), "model_key": args.model,
        "calls": runtime.calls, "sha256": digest(args.output)}))


if __name__ == "__main__":
    main()
