"""Exact focus deduplication in Amplifier's counted, post-compaction view.

This optional adapter wraps the advertised context capability, never a provider
or decoder. The callback keyword is a pinned loop implementation seam. Unknown
versions/signatures keep the full reminder. Canonical history is never edited.
"""
from copy import deepcopy
import hashlib
import importlib
import inspect
import json
from pathlib import Path


PREFIX = ('Current decision frame. Original request is authoritative; model hypotheses '
          'and retained UI are not approval or fresh verification.\n')
CAPABILITY = 'context.measured_request_view'
EVENT = 'context:focus_dedup'
SOURCE_PINS = {
    'amplifier_module_loop_streaming': '19a60b206f0ceaafacca10cc0788c53094d49a80b90f15f2c7c2da4c5938f406',
    'amplifier_module_context_simple': '7c5e0c26920cfc09f7b1cef09d81c04c92a604cb9b79c6279676cad81f9ba968',
}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def module_compatibility():
    """Pin code, not package versions (both upstream modules report 1.0.0)."""
    actual = {}
    try:
        for name, expected in SOURCE_PINS.items():
            module = importlib.import_module(name)
            actual[name] = hashlib.sha256(Path(inspect.getsourcefile(module)).read_bytes()).hexdigest()
            if actual[name] != expected:
                return False, {'reason': 'module_source_pin_mismatch', 'source_hashes': actual}
    except (ImportError, OSError, TypeError):
        return False, {'reason': 'module_source_pin_unavailable', 'source_hashes': actual}
    return True, {'reason': 'pinned_modules', 'source_hashes': actual}


def project_focus(body, base_view, registered):
    """Remove only complete ordered rows also present in a trusted tool receipt.

Comparisons use strict canonical JSON (including scalar types). Any truncation,
compaction removal, changed competitor/value, wrong view, or unregistered tool
result retains the full focus. Routes, constraints and authority never change.
"""
    report = {'deduplicated': False, 'reason': 'unrecognized_reminder'}
    if not isinstance(body, str) or not body.startswith(PREFIX):
        return body, report
    try:
        state = json.loads(body[len(PREFIX):])
        focus = state.get('focus') if isinstance(state, dict) else None
        if not isinstance(focus, dict) or not isinstance(focus.get('items'), list) or not focus['items']:
            return body, {**report, 'reason': 'no_full_focus_items'}
        original_bytes = len(body.encode())
        rows_hash = digest(focus['items'])
        for message in reversed(base_view):
            if not isinstance(message, dict) or message.get('role') != 'tool':
                continue
            call_id = message.get('tool_call_id')
            saved = registered.get(call_id) if isinstance(call_id, str) else None
            if not saved or saved['name'] not in ('locua_inspect', 'locua_search'):
                continue
            if message.get('name') != saved['name']:
                continue
            try:
                envelope = json.loads(message['content'])
            except (ValueError, TypeError, KeyError):
                continue
            if not isinstance(envelope, dict) or digest(envelope) != saved['digest'] or envelope.get('success') is not True:
                continue
            output = envelope.get('output')
            if not isinstance(output, dict) or output.get('status') not in ('ok', 'observed'):
                continue
            page = output.get('overview', output)
            if not isinstance(page, dict) or page.get('view') != focus.get('view'):
                continue
            if digest(page.get('items')) != rows_hash:
                continue
            result = deepcopy(state)
            del result['focus']['items']
            result['focus']['items_in_tool_result'] = {
                'tool_call_id': call_id, 'view': focus.get('view'), 'count': len(focus['items']),
                'sha256': rows_hash, 'complete_items_present_in_this_request': True,
            }
            reduced = PREFIX + compact(result)
            reduced_bytes = len(reduced.encode())
            if reduced_bytes >= original_bytes:
                return body, {**report, 'reason': 'reference_would_not_reduce_bytes'}
            return reduced, {
                'deduplicated': True, 'reason': 'exact_registered_rows_in_counted_view',
                'source_tool_call_id': call_id, 'view': focus.get('view'),
                'rows_sha256': rows_hash, 'rows': len(focus['items']),
                'original_reminder_bytes': original_bytes, 'reduced_reminder_bytes': reduced_bytes,
                'removed_reminder_bytes': original_bytes - reduced_bytes,
            }
    except (TypeError, ValueError, OverflowError):
        return body, {**report, 'reason': 'unsupported_reminder_or_view'}
    return body, {**report, 'reason': 'identical_registered_full_page_not_in_candidate_view'}


class MeasuredFocusAdapter:
    """One session-owned capability adapter. Never reassembles a counted request."""

    def __init__(self):
        self.registered = {}
        self.coordinator = None
        self.original = None
        self.wrapper = None
        self.unregister = None
        self.requests = 0
        self.closed = False

    async def mount(self, coordinator):
        from amplifier_core import HookResult
        self.coordinator = coordinator
        compatible, details = module_compatibility()
        original = coordinator.get_capability(CAPABILITY)
        if not compatible:
            await coordinator.hooks.emit(EVENT, {'stage': 'mount', 'deduplicated': False, **details})
            return False
        if (not callable(original)
                or getattr(original, '__module__', None) != 'amplifier_module_context_simple'):
            await coordinator.hooks.emit(EVENT, {
                'stage': 'mount', 'deduplicated': False, 'reason': 'capability_unavailable_or_replaced'})
            return False
        self.original = original

        async def remember(event, data):
            name = data.get('tool_name', data.get('name'))
            call_id = data.get('tool_call_id')
            result = data.get('result')
            if hasattr(result, 'model_dump'):
                result = result.model_dump()
            if name in ('locua_inspect', 'locua_search') and isinstance(call_id, str) and isinstance(result, dict):
                try:
                    self.registered[call_id] = {'name': name, 'digest': digest(result)}
                except (TypeError, ValueError, OverflowError):
                    self.registered.pop(call_id, None)
            return HookResult(action='continue')

        async def measured(*, provider, retain_contents, count_view):
            self.requests += 1
            request_index = self.requests
            try:
                parameter = inspect.signature(count_view).parameters.get('request_injection')
                default = parameter.default if parameter is not None else None
                supported = (getattr(count_view, '__module__', None) == 'amplifier_module_loop_streaming'
                    and parameter is not None and parameter.kind is inspect.Parameter.KEYWORD_ONLY
                    and isinstance(default, tuple) and len(default) == 2 and default[1] is False)
            except (TypeError, ValueError):
                supported = False
            if not supported:
                await coordinator.hooks.emit(EVENT, {'stage': 'request', 'request_index': request_index,
                    'deduplicated': False, 'reason': 'no_supported_tail_callback'})
                return await original(provider=provider, retain_contents=retain_contents, count_view=count_view)
            attempts = []

            async def candidate(base_view):
                body, receipt = project_focus(default[0], base_view, self.registered)
                # The official callback counts exactly this request and returns
                # its dispatch object. Never independently rebuild or recount it.
                envelope = await count_view(base_view, request_injection=(body, False))
                receipt.update(stage='candidate', request_index=request_index, attempt_index=len(attempts) + 1,
                               base_view_sha256=digest(base_view))
                if isinstance(envelope, dict):
                    budget = envelope.get('budget_decision')
                    if isinstance(budget, dict):
                        receipt['measurement'] = deepcopy(budget.get('measurement'))
                attempts.append((envelope, receipt))
                await coordinator.hooks.emit(EVENT, deepcopy(receipt))
                return envelope

            result = await original(provider=provider, retain_contents=retain_contents, count_view=candidate)
            final = result.get('final_attempt') if isinstance(result, dict) else None
            matched = next((receipt for envelope, receipt in reversed(attempts) if envelope is final), None)
            if matched is None:
                # A changed capability contract must not dispatch an unproven
                # reconstruction. The pinned context normally returns identity.
                transaction = result.get('transaction') if isinstance(result, dict) else None
                if transaction is not None:
                    transaction.rollback()
                raise RuntimeError('Focus context adapter lost the exact counted dispatch; no generation is permitted')
            await coordinator.hooks.emit(EVENT, {**matched, 'stage': 'selected', 'exact_counted_dispatch': True})
            return result

        self.wrapper = measured
        self.unregister = coordinator.hooks.register('tool:post', remember, name='locua-focus-source-receipts', priority=101)
        coordinator.register_capability(CAPABILITY, measured)
        coordinator.register_cleanup(self.cleanup)
        await coordinator.hooks.emit(EVENT, {'stage': 'mount', 'enabled': True, **details})
        return True

    async def cleanup(self):
        if self.closed:
            return
        self.closed = True
        if self.coordinator is not None and self.coordinator.get_capability(CAPABILITY) is self.wrapper:
            self.coordinator.register_capability(CAPABILITY, self.original)
        if callable(self.unregister):
            self.unregister()
        self.registered.clear()
