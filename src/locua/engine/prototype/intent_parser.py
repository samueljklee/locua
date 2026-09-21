"""Extractive compact proposals: finite source choices, deterministic envelope.

This does not infer UI hierarchy or prove natural-language authorization. It
preserves literal spans and refuses unsupported proposals; guards still ground
and verify them independently. Text values must be quoted or supplied as data.
"""
from copy import deepcopy
import hashlib
import json
import re

VERSION = "locua-compact-proposal-v3"
MAX_ITEMS = 8
MAX_SUBJECTS = 4096
MAX_LITERALS = 128
PLANES = ("editor_buffer", "display", "committed_document", "saved_output")
QUESTIONS = {
    "missing_value": "What exact text should be entered? Quote it or provide it as supplied data.",
    "ambiguous_target": "Which specific control or field should change? Supply its distinguishing label.",
    "conflicting_instructions": "The requested change conflicts with a preservation instruction. Which should take precedence?",
    "unsupported_operation": "This planner currently supports text and checkbox/selection changes. Navigation, save and other commands need a reviewed plan.",
    "unclear_evidence": "Should completion require displayed text, an exact editor buffer, a committed document or a saved output?",
    "unclear_request": "Please identify the requested field or state change more precisely.",
}


def _add(table, text, provenance):
    table.setdefault(text, [])
    if provenance not in table[text]:
        table[text].append(provenance)


def extract_sources(request, supplied_data=None):
    subjects, literals = {}, {}
    # Every contiguous word span is available. No verb, app, goal or gold-based
    # filtering selects plausible targets. Edge punctuation is not part of a word.
    boundaries = []
    for match in re.finditer(r"\S+", request):
        start, end = match.span()
        while start < end and request[start] in '\"\'“”‘’.,;:!?()[]{}':
            start += 1
        while end > start and request[end-1] in '\"\'“”‘’.,;:!?()[]{}':
            end -= 1
        if start < end:
            boundaries.append((start, end))
    if len(boundaries) > 90:
        raise ValueError("Request exceeds 90 source words; no subject inventory truncation")
    for i, (start, _) in enumerate(boundaries):
        for _, end in boundaries[i:]:
            _add(subjects, request[start:end], {"kind": "request_span", "start": start, "end": end})
    # Exact quoted bodies; no unescaping/normalization/whitespace trimming.
    quoted = re.compile(r'"((?:\\.|[^"\\])*)"|“([^”]*)”|(?<!\w)\'([^\']*)\'(?!\w)', re.DOTALL)
    for match in quoted.finditer(request):
        group = next(n for n in (1, 2, 3) if match.group(n) is not None)
        start, end = match.span(group)
        evidence = {"kind": "request_span", "start": start, "end": end, "quoted": True}
        _add(literals, request[start:end], evidence)
        if start != end:
            _add(subjects, request[start:end], evidence)
    def data(value, path):
        if isinstance(value, str):
            _add(literals, value, {"kind": "supplied_data_value", "path": path})
        elif isinstance(value, dict):
            for key, child in value.items():
                if key:
                    _add(subjects, key, {"kind": "supplied_data_key", "path": path + [key]})
                data(child, path + [key])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                data(child, path + [index])
    data(supplied_data or {}, [])
    if len(subjects) > MAX_SUBJECTS or len(literals) > MAX_LITERALS:
        raise ValueError("Source catalog capacity exceeded; no source choices dropped")
    if any(len(value) > 8192 for value in literals):
        raise ValueError("Source literal exceeds 8192 characters; no truncation")
    return {"subjects": subjects, "literals": literals,
            "coverage": {"subject_count": len(subjects), "literal_count": len(literals),
                         "omitted_choices": 0, "subject_basis": "all_contiguous_request_word_spans_and_supplied_data_keys",
                         "literal_basis": "exact_quoted_request_bodies_and_supplied_data_strings",
                         "unquoted_text_values_supported": False}}


def compile_proposal(proposal, request, scope, supplied_data=None):
    """Compile declared tuples. Only the optional question list defaults empty.

    The raw proposal is immutable. Missing set/keep lists cannot be inferred:
    absent preservation data must never be silently treated as permission.
    """
    if not isinstance(proposal, dict) or not {"set", "keep"} <= set(proposal) or set(proposal) - {"set", "keep", "ask"}:
        raise ValueError("Expected compact set/keep proposal with optional ask")
    ask = proposal.get("ask", [])
    for key in ("set", "keep", "ask"):
        value = ask if key == "ask" else proposal[key]
        if not isinstance(value, list) or len(value) > MAX_ITEMS:
            raise ValueError("Proposal lists exceed eight entries or are not lists")
    sources = extract_sources(request, supplied_data)
    def copied(evidence):
        span = evidence[0]
        if span["kind"] == "request_span":
            return request[span["start"]:span["end"]]
        if span["kind"] == "supplied_data_key":
            return span["path"][-1]
        value = supplied_data
        for key in span["path"]:
            value = value[key]
        return value
    outcomes, constraints, copies = [], [], []
    seen = set()
    for mode, output in (("set", outcomes), ("keep", constraints)):
        for row in proposal[mode]:
            expected = 4 if mode == "set" else 3
            if not isinstance(row, list) or len(row) != expected:
                raise ValueError("Proposal entry has wrong tuple shape")
            name, prop, value = row[:3]
            if not isinstance(name, str) or name not in sources["subjects"]:
                raise ValueError("Subject was not copied from request or supplied data")
            name = copied(sources["subjects"][name])
            if prop == "value":
                if not isinstance(value, str) or value not in sources["literals"]:
                    raise ValueError("Text value must be an exact quoted/supplied literal")
                value = copied(sources["literals"][value])
            elif prop in ("checked", "selected"):
                if type(value) is not bool:
                    raise ValueError("State value must be boolean")
            else:
                raise ValueError("Only text and checked/selected state proposals are supported")
            key = (mode, name, prop)
            if key in seen:
                raise ValueError("Duplicate proposal for the same subject/property")
            seen.add(key)
            item = {"subject": {"name": name}, "property": prop, "value": value, "source_text": request}
            if mode == "set":
                plane = row[3]
                if plane not in PLANES or (prop != "value" and plane != "display"):
                    raise ValueError("Unsupported property/evidence-plane combination")
                item.update(id="o" + str(len(output)+1), requires=[], evidence_plane=plane)
            output.append(item)
            copies.append({"mode": mode, "index": len(output)-1, "subject": sources["subjects"][name],
                           "value": sources["literals"][value] if prop == "value" else {"kind": "boolean_interpretation"},
                           "source_text": {"kind": "request_span", "start": 0, "end": len(request)}})
    for outcome in outcomes:
        for constraint in constraints:
            if outcome["subject"] == constraint["subject"] and outcome["property"] == constraint["property"]:
                if type(outcome["value"]) is not type(constraint["value"]) or outcome["value"] != constraint["value"]:
                    raise ValueError("Requested edit contradicts a preservation requirement; no automatic repair")
    if any(not isinstance(code, str) or code not in QUESTIONS for code in ask):
        raise ValueError("Unknown clarification code")
    if len(set(ask)) != len(ask):
        raise ValueError("Duplicate clarification codes")
    if not outcomes and not ask:
        raise ValueError("No requested edits or clarifications")
    plan = {"version": "locua-task-plan-v1", "request": request, "scope": deepcopy(scope),
            "outcomes": outcomes, "constraints": constraints,
            "unknowns": [QUESTIONS[code] for code in ask]}
    provenance = {"compiler": VERSION, "request_sha256": hashlib.sha256(request.encode()).hexdigest(),
                  "source_copies": copies, "coverage": sources["coverage"],
                  "source_catalog": sources, "invented_roles_or_ancestors": False,
                  "semantic_authorization_proven": False, "proposal_repaired": False,
                  "schema_defaults": [] if "ask" in proposal else [{"field": "ask", "value": [], "basis": "declared_optional_empty_question_list"}]}
    return plan, provenance


class SourceGrammar:
    """Field-local token tries, not a Cartesian product of complete JSON plans.

    Each grammar segment is a finite set of exact strings. Only admissible tokens
    at the current node receive logits; advancing commits one completed field.
    Qwen tokenization is checked for exact round-trip of every segment.
    """
    def __init__(self, tokenizer, sources, *, eos_ids):
        self.tokenizer, self.sources = tokenizer, sources
        self.eos_ids = set(eos_ids)
        if not self.eos_ids:
            raise ValueError("An explicit EOS set is required")
        self.stage, self.mode, self.count, self.prop = "open", "set", 0, None
        self.done, self.transitions, self.generated, self.eos_prefetch = False, 0, [], 0
        self.cache = {}
        self._enter()

    def _options(self):
        if self.stage == "open": return [('{"set":[', None)]
        if self.stage == "item":
            return [(']', "close")] + ([('[', "entry")] if self.count < MAX_ITEMS and self.sources["subjects"] else [])
        if self.stage == "required_entry": return [('[', "entry")]
        if self.stage == "subject": return [(json.dumps(s, ensure_ascii=False), s) for s in self.sources["subjects"]]
        if self.stage in ("comma_property", "comma_value", "comma_plane"): return [(',', None)]
        if self.stage == "property":
            values = ["value", "checked", "selected"] if self.sources["literals"] else ["checked", "selected"]
            return [(json.dumps(p), p) for p in values]
        if self.stage == "value":
            return ([(json.dumps(v, ensure_ascii=False), v) for v in self.sources["literals"]]
                    if self.prop == "value" else [('true', True), ('false', False)])
        if self.stage == "plane": return [(json.dumps(p), p) for p in (PLANES if self.prop == "value" else ("display",))]
        if self.stage == "end_entry": return [(']', None)]
        if self.stage == "after_entry": return [(']', "close")] + ([(',', "another")] if self.count < MAX_ITEMS else [])
        if self.stage == "next_array": return [(',"keep":[', "keep")] if self.mode == "set" else [(',"ask":[', "ask")]
        if self.stage == "question": return [(']', "close")] + [(json.dumps(q), q) for q in QUESTIONS if q not in self.questions]
        if self.stage == "after_question": return [(']', "close")] + ([(',', "another")] if len(self.questions) < MAX_ITEMS else [])
        if self.stage == "question_required": return [(json.dumps(q), q) for q in QUESTIONS if q not in self.questions]
        if self.stage == "end": return [('}', None)]
        raise ValueError("Unknown grammar state: " + self.stage)

    def _enter(self):
        if self.done:
            return
        options = self._options()
        # Identical punctuation has different transition meanings in different
        # states (entry ']' ends a row, array ']' closes a list). Cache both.
        key = tuple((text, json.dumps(value, sort_keys=True)) for text, value in options)
        if key not in self.cache:
            root = {}
            for text, value in options:
                tokens = self.tokenizer.encode(text, add_special_tokens=False)
                if not tokens or self.tokenizer.decode(tokens) != text or any(t in self.eos_ids for t in tokens):
                    raise ValueError("Grammar segment failed exact tokenizer round-trip")
                node = root
                for token in tokens:
                    node = node.setdefault(token, {})
                if node or "end" in node:
                    raise ValueError("Ambiguous grammar token sequence")
                node["end"] = value
            self.cache[key] = root
        self.node = self.cache[key]

    def allowed(self):
        return self.eos_ids if self.done else set(self.node)

    def advance(self, token):
        if token not in self.allowed():
            raise ValueError("Token is outside the declared source grammar")
        if self.done:
            # MLX generate_step evaluates one token ahead before yielding EOS.
            # Keep the terminal state absorbing and admit only EOS here.
            self.eos_prefetch += 1
            return
        self.generated.append(token)
        self.node = self.node[token]
        if "end" not in self.node:
            return
        if len(self.node) != 1:
            raise ValueError("A grammar segment is a prefix of another")
        value = self.node["end"]
        stage = self.stage
        self.transitions += 1
        if stage == "open": self.stage = "item"
        elif stage == "item": self.stage = "next_array" if value == "close" else "subject"
        elif stage == "subject": self.stage = "comma_property"
        elif stage == "comma_property": self.stage = "property"
        elif stage == "property": self.prop, self.stage = value, "comma_value"
        elif stage == "comma_value": self.stage = "value"
        elif stage == "value": self.stage = "comma_plane" if self.mode == "set" else "end_entry"
        elif stage == "comma_plane": self.stage = "plane"
        elif stage == "plane": self.stage = "end_entry"
        elif stage == "end_entry": self.count += 1; self.stage = "after_entry"
        elif stage == "after_entry": self.stage = "next_array" if value == "close" else "required_entry"
        elif stage == "required_entry": self.stage = "subject"
        elif stage == "next_array":
            self.mode, self.count = value, 0
            self.stage = "question" if value == "ask" else "item"
            self.questions = []
        elif stage in ("question", "question_required"):
            if value == "close": self.stage = "end"
            else: self.questions.append(value); self.stage = "after_question"
        elif stage == "after_question": self.stage = "end" if value == "close" else "question_required"
        elif stage == "end": self.done = True
        else: raise ValueError("Unhandled grammar transition")
        self._enter()

    def report(self):
        return {"mechanism": "field_local_token_trie", "complete": self.done,
                "grammar_version": "source-grammar-v3-transition-cache",
                "grammar_tokens": len(self.generated), "field_transitions": self.transitions,
                "omitted_source_choices": 0, "terminal_eos_prefetch": self.eos_prefetch}


class MLXSourceMask:
    """MLX logits processor; initial tokens may contain only the prefill suffix."""
    def __init__(self, grammar, prompt_tokens, mx, check_deadline):
        self.grammar, self.prompt, self.mx, self.check = grammar, prompt_tokens, mx, check_deadline
        self.previous = None

    def __call__(self, tokens, logits):
        self.check()
        values = tokens.tolist()
        if self.previous is None:
            if not values or values != self.prompt[-len(values):]:
                raise ValueError("Generation prefix differs from the supplied prompt suffix")
        else:
            if len(values) != len(self.previous)+1 or values[:-1] != self.previous:
                raise ValueError("Generation token stream was rewound, skipped or replaced")
            self.grammar.advance(values[-1])
        self.previous = values
        allowed = sorted(self.grammar.allowed())
        if not allowed or min(allowed) < 0 or max(allowed) >= logits.shape[-1]:
            raise ValueError("No valid grammar token within model vocabulary")
        mask = self.mx.full(logits.shape[-1], -float("inf"), dtype=logits.dtype)
        mask[self.mx.array(allowed)] = 0
        return logits + mask
