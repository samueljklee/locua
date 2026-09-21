"""Observed-field ordinary generation; original RLCD and old planner unchanged."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import time

from .observed_planner import VERSION, DECODING, SYSTEM_PROMPT, messages_for
from .planner import (MAX_GENERATION_SECONDS, MAX_INPUT_TOKENS, MAX_JSON_BYTES, MAX_OUTPUT_TOKENS,
                      PLANNING_MODE, strict_json, validate_limits)
from .planner_worker import LocalRuntime, GenerationDeadline, hard_watchdog
from .decision import MemoryConfig


def generate_plan(runtime, request, scope, catalog, supplied_data=None, *, max_input_tokens=MAX_INPUT_TOKENS,
                  max_output_tokens=MAX_OUTPUT_TOKENS, generation_timeout_s=MAX_GENERATION_SECONDS,
                  watchdog_factory=None):
    """Pure orchestration boundary for actual runtime or model-free test double.

    Runtime.stream is called once, never repaired or retried. The actual worker
    supplies a process-exit watchdog for a GPU call that cannot yield promptly.
    """
    validate_limits(max_input_tokens, max_output_tokens, generation_timeout_s)
    started = time.perf_counter()
    messages = messages_for(request, scope, catalog, supplied_data)
    prompt_tokens = runtime.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    if not isinstance(prompt_tokens, list) or any(type(v) is not int for v in prompt_tokens):
        raise ValueError("Tokenizer must produce one explicit list of input token IDs")
    if not 0 < len(prompt_tokens) <= max_input_tokens:
        raise ValueError(f"Planner input {len(prompt_tokens)} exceeds {max_input_tokens} tokens or is empty; no truncation")
    prompt_sha = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
    generation_started = time.perf_counter()
    deadline = time.monotonic() + generation_timeout_s
    def check_deadline(*_):
        if time.monotonic() >= deadline:
            raise GenerationDeadline("Planner generation deadline reached; no retry")
    watchdog = watchdog_factory(generation_timeout_s) if watchdog_factory is not None else None
    if watchdog is not None:
        watchdog.start()
    chunks, output_tokens, finish_reason, stream, generation_error = [], 0, None, None, None
    generation_calls = 0
    try:
        check_deadline()
        generation_calls += 1
        stream = runtime.stream(prompt_tokens, max_tokens=max_output_tokens, check_deadline=check_deadline)
        for response in stream:
            check_deadline()
            text = response.text
            count = response.generation_tokens
            if not isinstance(text, str) or type(count) is not int or not output_tokens <= count <= max_output_tokens:
                raise ValueError("Generation stream violated output token/text contract")
            output_tokens = count
            chunks.append(text)
            if sum(len(c.encode()) for c in chunks) > MAX_JSON_BYTES:
                raise ValueError("Planner output exceeds byte limit; no truncation")
            if response.finish_reason is not None:
                if response.finish_reason not in ("stop", "length"):
                    raise ValueError("Unknown generation finish reason")
                finish_reason = response.finish_reason
                break
        check_deadline()
        if finish_reason is None:
            generation_error = {"type": "IncompleteGeneration", "message": "Stream ended without a completion reason"}
            finish_reason = "incomplete"
    except GenerationDeadline as exc:
        finish_reason = "timeout"
        generation_error = {"type": type(exc).__name__, "message": str(exc)}
    except Exception as exc:
        finish_reason = "error"
        generation_error = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            if stream is not None and hasattr(stream, "close"):
                stream.close()
            check_deadline()
        except GenerationDeadline as exc:
            finish_reason = "timeout"
            generation_error = {"type": type(exc).__name__, "message": str(exc)}
        except Exception as exc:
            finish_reason = "error"
            generation_error = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            if watchdog is not None:
                watchdog.cancel()
            generation_ms = (time.perf_counter() - generation_started) * 1000
            try:
                runtime.clear_idle_cache()
            except Exception as exc:
                finish_reason = "error"
                generation_error = {"type": type(exc).__name__, "message": str(exc)}
    return {"raw_output": "".join(chunks), "finish_reason": finish_reason,
            "generation_error": generation_error, "generation_calls": generation_calls,
            "proposal_version": VERSION, "planner_policy": VERSION, "planner_decoding": DECODING,
            "grammar": runtime.grammar_report() if hasattr(runtime, "grammar_report") else {"test_double": True},
            "planning_mode": PLANNING_MODE, "dispatched": False,
            "usage": {"input_tokens": len(prompt_tokens), "output_tokens": output_tokens},
            "timing": {"generation_ms": round(generation_ms, 3),
                       "worker_total_ms": round((time.perf_counter() - started) * 1000, 3)},
            "model_info": runtime.info(), "prompt_sha256": prompt_sha,
            "input_limit_tokens": max_input_tokens, "output_limit_tokens": max_output_tokens,
            "generation_limit_seconds": generation_timeout_s}

class ObservedRuntime(LocalRuntime):
    def __init__(self, model, limits):
        super().__init__(model, limits, decoder="compact_greedy")
        self.metadata.update(service=VERSION, decoder=DECODING, proposal_version=VERSION,
            planner_policy=VERSION, decoder_version=DECODING, logits_constraint="none",
            source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            intent_parser_sha256=hashlib.sha256(Path(__file__).with_name("observed_planner.py").read_bytes()).hexdigest(),
            prompt_sha256=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest())


def serve(runtime, input_stream, output_stream, *, watchdog_factory=None):
    def emit(value):
        output_stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+"\n"); output_stream.flush()
    emit({"type":"ready","info":runtime.info()}); seen=set()
    while True:
        line=input_stream.readline(MAX_JSON_BYTES+4097)
        if not line: return
        if len(line.encode())>MAX_JSON_BYTES+4096 or not line.endswith("\n"):
            emit({"type":"fatal","error":"Oversized/incomplete planner request"}); return
        message={}
        try:
            message=strict_json(line)
            if not isinstance(message,dict) or not isinstance(message.get("id"),str) or not message["id"] or message["id"] in seen:
                raise ValueError("Invalid/repeated request identity")
            seen.add(message["id"])
            if message.get("op")=="shutdown":
                emit({"id":message["id"],"ok":True}); return
            if message.get("op")=="info":
                payload={"info":runtime.info()}
            elif message.get("op")=="plan":
                if set(message)!={"id","op","request","scope","catalog","supplied_data"}:
                    raise ValueError("Unknown or missing planner request fields")
                with contextlib.redirect_stdout(sys.stderr):
                    result=generate_plan(runtime,message["request"],message["scope"],message["catalog"],message["supplied_data"],watchdog_factory=watchdog_factory)
                payload={"planning":result}
            else: raise ValueError("Unknown planner operation")
            emit({"id":message["id"],"ok":True,**payload})
        except Exception as error:
            emit({"id":message.get("id") if isinstance(message,dict) else None,"ok":False,
                  "error":{"type":type(error).__name__,"message":str(error)}})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model",choices=("baseline","comparator"),default="comparator")
    args=parser.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        runtime=ObservedRuntime(args.model,MemoryConfig())
    serve(runtime,sys.stdin,sys.stdout,watchdog_factory=hard_watchdog)


if __name__=="__main__": main()
