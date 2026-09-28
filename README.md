# Locua

Locua is a developer preview of a local computer-use agent. Describe an outcome
in ordinary language; the model discovers the application and UI, proposes a
readable review, performs guarded actions, and checks the requested result.

```sh
locua "Open Calendar and switch to Day view."
```

The current native preview defaults to **local Qwen3.8-27B** (`qwen38`),
**step-v2** tools, and **continuity-v1** instructions through Amplifier's standard
loop. It uses ordinary, nonthinking tool calling. **This default does not use RLCD.**
No control IDs, task schema, or prepared action sequence are required.

This is not a generally reliable agent: completion-rate and latency qualification
remain open. See [known limitations](src/locua/docs/gaps.md).

## Already configured?

Activate the Python environment where you installed Locua, then run:

```sh
locua --version
locua doctor
locua "Open Calendar and switch to Day view."
```

The preview release is `0.2.0`. `locua` with no arguments asks for an outcome.
`locua start "Open Calendar and switch to Day view."` uses the same defaults.
If your shell cannot find `locua`, activate the installation environment or use
its `bin/locua` executable directly. No particular working directory is required.

## First-time installation

Live execution currently targets **Apple Silicon macOS**. Python 3.11 or newer
is required for the CLI; the local MLX runtime has its own pinned dependencies.
Windows and Linux execution are not validated.

You must provision three components:

1. This CLI with its Amplifier dependencies.
2. A local MLX runtime and the exact cached model revision.
3. A compatible Cua Driver build, its socket, and the required macOS permissions.

**Installing the Python package alone does not provide a working desktop agent.**
There is not yet an end-to-end installer for the model and driver. Native editor
features require Locua's pinned driver contract; an arbitrary upstream driver
build is not guaranteed compatible. See the
[installation guide](src/locua/docs/installation.md) and
[native editor contract](src/locua/engine/prototype/native_driver_contract.py).

From an authorized checkout of this repository:

```sh
git clone https://github.com/samueljklee/locua.git
cd locua
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[amplifier]'
locua --version
```

Cloning requires repository access while it is private. This installs code
dependencies and the `locua` executable; it does not download model weights.
For the separately provisioned MLX runtime, dependency versions are recorded in
[requirements-rlcd.lock.txt](src/locua/engine/probes/requirements-rlcd.lock.txt).
The default 27B model's revision and memory guideline are recorded in
[qwen38-model.json](src/locua/engine/probes/qwen38-model.json), with verified asset
hashes in [qwen38-files.json](src/locua/engine/probes/qwen38-files.json).
Model files are approximately 16 GB; runtime memory requirements are additional.
Suitability for smaller-memory Macs remains unvalidated.

Once those assets exist, replace these placeholder paths with your own:

```sh
locua setup \
  --runtime-python /absolute/path/to/model-runtime/bin/python \
  --model-cache /absolute/path/to/huggingface \
  --driver-binary /absolute/path/to/CuaDriver \
  --driver-socket /absolute/path/to/driver.sock
locua doctor
locua doctor --probe
```

`--model-cache` names the Hugging Face home containing `hub/`, not `hub/` itself.
`setup` writes local configuration; it does not install or start the driver by
default. Run the configured driver with its required macOS Accessibility and
Screen Recording grants. For a provisioned application bundle, see
`locua setup --help` for explicit `--driver-app` / `--activate-driver` support.
`doctor --probe` checks configured runtime/driver readiness without loading a model;
a passing diagnostic is not evidence that a task will complete.

## Run, review, and understand the result

```sh
locua "Open Calendar and switch to Day view."
```

Locua reports the selected model, tools and decoding method, followed by live tool
progress. It may discover, open or activate the requested app before review.
When the readable review appears, check that it covers the **entire request and
its constraints**, then type `run` to allow task input. Another response cancels.
Answer a clarification only when the requested target or outcome is ambiguous.

The final output reports a verified reviewed outcome or the specific incomplete
outcome, along with timing and the session-log location. A successful app launch,
a valid plan or the model saying “done” does not count as task completion.
Verification covers declared outcomes and constraints; it does not prove the
absence of every unobserved side effect.

Start with disposable tasks. A running application may still have no visible,
readable window. If Calendar discovery reports an accessibility/readability
failure, reopen the window normally and bring it onto the current desktop.
Automatic recovery from hidden/off-Space windows remains a known gap.

Only one Locua desktop-control session may run at a time. If another session owns
the desktop, wait for it or cancel it normally. After changing monitor layout,
stop active sessions before restarting the configured driver; dynamic overlay
geometry remains unvalidated. The overlay marks inspected UI evidence, not the
model's internal attention.

## Model and profile overrides

No extra flags are needed for the selected preview. To make it explicit:

```sh
locua "Open Calendar and switch to Day view." \
  --model qwen38 --tool-profile step-v2 --instruction-profile continuity-v1
```

To test a smaller local model with the same tools and instructions:

```sh
locua "Open Calendar and switch to Day view." --model comparator
```

`comparator` selects local 7B; `baseline` selects local 1.5B. These models remain
comparison options, not equally qualified alternatives. Missing assets stop the
run; Locua never silently chooses another model or hosted provider.

The original RLCD selection paths remain available through legacy/manual/reviewed
adapters. Selecting a model alone does not enable RLCD in the native Amplifier
loop. `start --manual` is an optional debug field picker, not the intended user
experience. Explicit `--url` and `--document` use separate limited adapters.
See [profiles and compatibility](src/locua/docs/profiles.md) before choosing them.

## Logs, help, and privacy

On macOS, configuration defaults to
`~/Library/Application Support/locua/config.json`; session logs go under
`~/Library/Application Support/locua/runs/`. Use `--config FILE` for another
configuration and `--out NEW_DIRECTORY` for a new run directory. `--json` returns
a structured final result; progress and review remain on stderr.

Logs can contain screen text, model input/output and document values. **Do not
commit or share raw logs without reviewing and redacting them.** Local inference
does not send observations to a hosted model. Hosted comparisons require explicit
provider/model selection and separate credentials; there is no automatic fallback.

```sh
locua -h                 # Short command guide
locua do --help          # Full language-task guide
locua setup --help       # Runtime and driver configuration
```

Before publishing source changes, run `python tools/privacy_check.py --staged`.
Use `--history` for a separate reachable-history audit. This redacted heuristic
check complements manual review; it is not a guarantee against every private fact.

## Development and release status

The public library is `locua.lib`; the CLI is its I/O adapter. The repository
contains the guarded engine, Cua adapters, Amplifier integration and isolated
evaluation tools. [AGENTS.md](AGENTS.md) lists development checks and invariants.

Known gaps include window recovery, model reliability and speed, monitor changes,
browser/Office transfer, generic saved-file verification and other platforms.
Exact editor-buffer verification does not prove saved-file contents. The full
8/10 per-workflow acceptance target and typical task latency under two minutes
have not been established. See [the gap list](src/locua/docs/gaps.md).

A project-level license has not yet been selected. Bundled third-party code keeps
its own [license](src/locua/engine/vendor/qwen/LICENSE.txt) and
[notice](src/locua/engine/vendor/qwen/NOTICE.txt); those notices do not select a
license for the rest of Locua. Model weights and the driver are separately
provisioned assets with their own licensing terms.
