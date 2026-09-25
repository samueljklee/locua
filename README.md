# Locua

Describe an outcome. Locua uses a local model to discover the application and UI,
prepare a readable review, execute guarded actions, and check the requested result.
This is a developer preview on Apple Silicon macOS, not a generally reliable agent.

```sh
locua "Open Calendar and switch to Day view."
```

Version **0.2.0** makes the selected preview configuration the default:
**local Qwen3.8-27B** (`qwen38`), **step-v2** tools and **continuity-v1** instructions,
through Amplifier's standard loop. It uses ordinary, nonthinking tool calling;
**RLCD is not used in this path**. No profile flags or internal task schema are needed.
The user's successful Calendar retry motivated this selection; it is not an 8/10
reliability qualification or a claim that every task will finish within two minutes.

Before task input, check that the readable review covers the entire request and
its constraints, then type `run`. Discovery and requested app activation can occur
before review. Progress shows tool activity, refusals and the selected configuration.
The final result reports verified outcomes or the specific incomplete outcome.
An app launch or a model's success statement is not completion.

Install and configure the local runtime, cached model and Cua Driver using the
[installation guide](src/locua/docs/installation.md), then run `locua doctor`.
Installation does not download model weights or grant OS permissions.
On an already configured installation, no setup changes are needed for this release.

```sh
locua                         # Ask for an outcome interactively
locua start "Open Calendar and switch to Day view."
locua "Open Calendar and switch to Day view." --model comparator
```

The last command explicitly tests the smaller local 7B with the same default tools
and instructions; it is not equally qualified. `--model baseline` retains 1.5B.
[Model and profile overrides](src/locua/docs/profiles.md) document the historical
interfaces, hosted comparisons and unchanged original RLCD paths. Missing model
assets stop the command; there is no provider or model fallback.

[Known gaps and next checkpoint](src/locua/docs/gaps.md) distinguish window recovery,
model decisions, verification, latency and untested applications. In particular,
a running process with an invisible window can still block Calendar discovery.
Do not force-kill personal applications as an automated workaround.

Private session logs default to `~/Library/Application Support/locua/runs/` on
macOS. `--out NEW_DIRECTORY` selects another location and `--json` emits the full
result. Logs may contain UI content and stay out of Git. `locua -h` is the concise
help; `locua --help` and `locua <capability> --help` expose the packaged Smart Tool guide.

The public library is `locua.lib`; the CLI is its I/O adapter. Source includes the
guarded engine, Cua adapters, Amplifier integration and isolated evaluation tools.
Developer checks and invariants are in [AGENTS.md](AGENTS.md). The private repository
is [samueljklee/locua](https://github.com/samueljklee/locua); no package registry
release is implied by the locally installed wheel.
