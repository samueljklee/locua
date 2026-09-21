"""Explicit dependency paths and integrity checks for bundled inference code."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def runtime_python():
    value = os.environ.get("LOCUA_RUNTIME_PYTHON")
    if not value or not Path(value).is_file():
        raise RuntimeError("Configure runtime_python to the local Python environment with the pinned MLX dependencies")
    return value


def model_cache():
    value = os.environ.get("LOCUA_MODEL_CACHE")
    if not value or not Path(value).is_dir():
        raise RuntimeError("Configure model_cache to the local Hugging Face cache with pinned model snapshots")
    return Path(value)


def worker_environment(env):
    env = dict(env)
    # A configured Python may be in another environment. Import this installed
    # package's code there explicitly; no developer checkout is discovered.
    env["PYTHONPATH"] = str(ROOT.parents[1])
    env["HF_HOME"] = str(model_cache())
    env["HF_HUB_OFFLINE"] = env["TRANSFORMERS_OFFLINE"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    return env


def verify_source():
    vendor = ROOT / "vendor/qwen"
    source = json.loads((vendor / "source.json").read_text())
    pin = json.loads((ROOT / "probes/rlcd-model.json").read_text())
    if source["revision"] != pin["upstream_revision"]:
        raise RuntimeError("Bundled RLCD source revision differs from the original pin")
    for name, expected in source["files"].items():
        if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Bundled RLCD source changed: " + name)
    return source


def verify_model(key):
    if key not in ("baseline", "comparator"):
        raise ValueError("Select the original baseline or explicit comparator")
    record = json.loads((ROOT / "probes" / (key + "-files.json")).read_text())
    pin = record["model"]
    expected = json.loads((ROOT / "probes/model_compare_v3_models.json").read_text())["models"][key]
    if any(pin.get(field) != expected[field] for field in ("model_id", "revision")):
        raise RuntimeError("Local model file manifest differs from the configured model pin")
    names = [item["path"] for item in record["files"]]
    if (not names or len(names) != len(set(names))
            or not {"config.json", "tokenizer.json", "tokenizer_config.json"} <= set(names)
            or not any(name.endswith(".safetensors") for name in names)
            or any(Path(name).name != name for name in names)):
        raise RuntimeError("Local model file manifest is incomplete or contains duplicate/unsafe paths")
    snapshot = model_cache() / "hub" / ("models--" + pin["model_id"].replace("/", "--")) / "snapshots" / pin["revision"]
    for item in record["files"]:
        path = snapshot / item["path"]
        if path.stat().st_size != item["bytes"]:
            raise RuntimeError("Pinned local model file size differs: " + str(path))
        with path.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if actual != item["sha256"]:
            raise RuntimeError("Pinned local model digest differs: " + str(path))
    return snapshot
