"""Portable explicit configuration; no discovery of developer checkouts."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile

from .errors import LocuaError

PATH_FIELDS = ("runtime_python", "model_cache", "driver_binary", "driver_socket", "driver_app")
DEFAULTS = {"version": 1, "model_backend": "mlx", **dict.fromkeys(PATH_FIELDS)}
MAX_JSON_BYTES = 1024 * 1024


def default_path():
    """Conventional per-user config location, never the installed package tree."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/locua/config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "locua/config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "locua/config.json"


def selected_path(path=None):
    return Path(path or os.environ.get("LOCUA_CONFIG") or default_path()).expanduser().resolve()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError("Nonfinite JSON value: " + value)
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    json.dumps(value, allow_nan=False)
    return value


def read_json(path):
    p = Path(path).expanduser()
    try:
        with p.open("rb") as file:
            content = file.read(MAX_JSON_BYTES + 1)
        if len(content) > MAX_JSON_BYTES:
            raise ValueError("JSON file exceeds 1 MiB; no truncation")
        return strict_json(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise LocuaError("invalid_json_file", str(error), "Supply a readable UTF-8 JSON file of at most 1 MiB.", exit_code=2) from error


def validate(value, *, base=None):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise LocuaError("invalid_config", "Configuration has unknown fields or is not an object.",
                         "Use locua setup --help for the supported fields.", exit_code=2)
    result = {**deepcopy(DEFAULTS), **deepcopy(value)}
    if type(result["version"]) is not int or result["version"] != 1 or result["model_backend"] != "mlx":
        raise LocuaError("unsupported_config", "Require config version 1 and model_backend='mlx'.",
                         "Use the supported local MLX backend; online fallbacks are forbidden.", exit_code=2)
    base = Path(base or Path.cwd())
    for field in PATH_FIELDS:
        raw = result[field]
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise LocuaError("invalid_config_path", field + " must be a nonempty local path or null.",
                             "Set the explicit path with locua setup.", exit_code=2)
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        # A venv interpreter is commonly a symlink. Dereferencing it changes
        # Python's pyvenv.cfg discovery and silently selects the base environment.
        result[field] = os.path.abspath(path) if field == "runtime_python" else str(path.resolve())
    return result


def load(path=None, *, required=False):
    """Return (normalized configuration, selected path), without provisioning."""
    p = selected_path(path)
    if not p.exists():
        if required:
            raise LocuaError("configuration_missing", "No Locua configuration exists at " + str(p),
                             "Run locua setup with explicit local runtime/model/driver paths.")
        return deepcopy(DEFAULTS), p
    return validate(read_json(p), base=p.parent), p


def write(value, path=None, *, replace=False):
    """Write an owner-readable config file. Replacement requires explicit opt-in."""
    p = selected_path(path)
    data = validate(value, base=p.parent)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if not replace:
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError as error:
            raise LocuaError("configuration_exists", "Configuration already exists at " + str(p),
                             "Review it, then use locua setup --replace if replacement is intended.", exit_code=2) from error
    else:
        fd, temporary = tempfile.mkstemp(prefix=".locua-config-", dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, p)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return data, p
