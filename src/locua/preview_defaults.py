"""Public preview defaults; explicit selections are never replaced or retried."""

MODEL = "qwen38"
TOOL_PROFILE = "step-v2"
INSTRUCTION_PROFILE = "continuity-v1"


def resolve(*, model=None, tool_profile=None, instruction_profile=None,
            provider="local", harness="amplifier", url=None, document=None,
            task_observations=False):
    """Resolve omissions only. Legacy and hosted adapters keep their contracts.

    This is configuration selection before execution, never a runtime fallback.
    The caller validates explicit combinations against their supported routes.
    """
    native = harness == "amplifier" and url is None and document is None
    compact = native and provider == "local" and not task_observations
    if model is None and provider == "local":
        model = MODEL if native else "comparator"
    if tool_profile is None:
        tool_profile = TOOL_PROFILE if compact else "baseline"
    if instruction_profile is None:
        instruction_profile = INSTRUCTION_PROFILE if compact else "baseline"
    return model, tool_profile, instruction_profile
