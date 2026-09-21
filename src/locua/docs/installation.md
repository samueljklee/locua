# Installation and explicit runtime configuration

Install from this Git checkout with Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[amplifier]'
.venv/bin/locua --help
```

On Windows, virtual-environment executables live under `Scripts`; native/model
execution on Windows and Linux is not validated. A repository URL has not been
published yet. Once published, normal `pip install git+https://...@<revision>`
installation uses the same `pyproject.toml`; no global script installer is needed.

The package owns its engine sources. It does not search for a sibling lab checkout
or a developer's home directory. Model weights and native executables are external
runtime assets, explicitly selected in configuration. Do not embed weights or
private traces in a wheel.

The `amplifier` extra installs Core 1.6.1 and pinned standard loop/context modules.
It is required for the default native language CLI. Base installation still
supports deterministic commands and the explicit legacy/manual engine. Installing
the extra fetches code dependencies, not model weights; inference stays offline.
For an offline installation, first build/download the pinned dependency wheels
on a connected machine and install them with `--no-index --find-links`.

```sh
locua setup --runtime-python /path/to/local-runtime/bin/python \
  --model-cache /path/to/huggingface \
  --driver-binary /path/to/CuaDriver \
  --driver-socket /path/to/driver.sock
locua doctor
```

`setup` only writes configuration unless `--activate-driver` is explicitly supplied.
It does not install packages, download weights, or grant permissions. Existing
configuration is preserved unless `--replace` is supplied. Optional `--driver-app`
records the distinct local application bundle used for explicit activation.
Run `locua setup --help` for that action's boundaries.

`model_cache` is the Hugging Face home directory containing `hub/models--...`,
not the `hub` directory itself. It is used as `HF_HOME` by local workers.

The configuration schema is version 1 with `model_backend: "mlx"` and optional
paths `runtime_python`, `model_cache`, `driver_binary`, `driver_socket`, and
`driver_app`. Relative paths in an existing config resolve against its directory.
The runtime interpreter's lexical absolute path is preserved: following a venv
Python symlink to its base executable would lose the configured environment.
CLI overrides are literal filesystem paths, not shell commands. Configuration is
selected by `--config`, then `LOCUA_CONFIG`, then the user's conventional config
directory. No secrets or online provider credentials are needed.

Use the engine's recorded model pins and dependency lock when provisioning the
runtime. Baseline means the exact original RLCD implementation and its own selected
Qwen2.5-1.5B weights; the repository name `Qwen-2.5-1B-RLCD` is not evidence of a
separate verified fine-tuned weight release. Comparator means the explicit pinned
Qwen2.5-7B experiment. Runtime failures must name the missing asset and stop.

Native Amplifier mode also accepts the explicit experimental `--model qwen38`:
`mlx-community/Qwen3.8-27B-4bit`, revision
`10c35caafbb80f7dc6a7a432cdd11af10a6d4818`. It requires the separately provisioned
files listed in the packaged `engine/probes/qwen38-files.json` manifest (about
16 GB). Loading verifies these hashes and the supported local MLX model class;
it never downloads assets or enables remote code during a task. This is a
text-only, nonthinking ordinary tool-calling comparison, not an RLCD replacement
or proof of suitability for ordinary Macs. No cloud fallback is configured.

macOS permissions belong to each application's own identity. A signed Cua
installation, a rebuilt driver, and a separate native AX helper do not inherit one
another's grants. `doctor --probe` checks configured components without requesting
new permissions or loading a model. Status success is not a completed UI task.
