"""Explicit hosted comparison configuration over the official Amplifier modules.

No client, message conversion, tool parser or provider fallback is implemented
here. Instance-local dispatch decorators enforce the comparison's finite bounds,
including upstream OpenAI's otherwise automatic continuation/output escalation.
"""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import fcntl
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import stat
import time
import uuid

VERSION = 'locua-hosted-comparison-v1'
MAX_INPUT = 24576
MAX_OUTPUT = 2048
CONFIGURATIONS = {
    'openai': {'model': 'gpt-5.6-sol', 'module': 'amplifier_module_provider_openai',
        'revision': 'e5c2f62df605a231a81c2407b7c2b5a6422e3a75', 'sdk': 'openai', 'sdk_version': '3.16.2',
        'package_source_sha256': 'a17f00ed93c0d299df4e509a4565edbd4e81e186e61c4c8bdccc62c3e0117746', 'credential': 'OPENAI_API_KEY', 'input_ceiling_per_million': '5', 'output_per_million': '20',
        'pricing_source': 'https://developers.openai.com/api/docs/models/gpt-5.6-sol',
        'config': {'default_model': 'gpt-5.6-sol', 'reasoning_effort': 'medium', 'max_output_tokens': MAX_OUTPUT,
            'max_retries': 0, 'use_streaming': False, 'timeout': 120, 'close_timeout': 5,
            'max_concurrent_requests': 1, 'raw': True, 'truncation': 'disabled',
            'extra_request_params': {'service_tier': 'default'}}},
    'anthropic': {'model': 'claude-opus-5', 'module': 'amplifier_module_provider_anthropic',
        'revision': '082f5c49c75dc4efe1fb4415bb89cee3bc349530', 'sdk': 'anthropic', 'sdk_version': '1.7.0',
        'package_source_sha256': '33e69732bf3cedffedf2f5ad3493abccccaee0e39b935d1adcd6e16a8cfdfedd', 'credential': 'ANTHROPIC_API_KEY', 'input_ceiling_per_million': '10', 'output_per_million': '25',
        'pricing_source': 'https://platform.claude.com/docs/en/models/opus-5/overview',
        'config': {'default_model': 'claude-opus-5', 'reasoning_effort': 'medium', 'max_tokens': MAX_OUTPUT,
            'thinking_type': 'adaptive', 'thinking_budget_tokens': 1024, 'thinking_budget_buffer': 1024,
            'max_retries': 0, 'use_streaming': False, 'timeout': 120, 'close_timeout': 5,
            'max_concurrent_requests': 1, 'raw': True, 'fallback_on_overload': False,
            'refusal_fallback_enabled': False, 'persist_fallback_state': False,
            'enable_web_search': False, 'cache_stable_region_ttl_1h': False}},
}


class HostedLimitError(ValueError):
    """A comparison bound refused dispatch; never a model decision failure."""


def validate_selection(provider, model):
    if provider not in CONFIGURATIONS or model != CONFIGURATIONS[provider]['model']:
        raise ValueError('Hosted comparison requires openai/gpt-5.6-sol or anthropic/claude-opus-5 explicitly')


def _data(value):
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else deepcopy(value)


def _encoded(value):
    return json.dumps(_data(value), sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as file:
        file.write(_encoded(value))


def load_credential(name, path=None):
    """Read only the requested assignment; never source/expand the env file."""
    if name not in {c['credential'] for c in CONFIGURATIONS.values()}:
        raise ValueError('Unsupported credential name')
    path = Path(path) if path else Path.home()/'.amplifier/keys.env'
    values = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith('export '): line = line[7:].lstrip()
        if '=' not in line or line.startswith('#'): continue
        key, value = line.split('=', 1)
        if key.strip() != name: continue
        value = value.strip()
        try:
            if value.startswith(('"', "'")): value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            raise ValueError('Invalid requested credential assignment') from None
        if (not isinstance(value, str) or not value or any(c.isspace() for c in value)
                or any(c in value for c in ('$', '`', '\x00'))):
            raise ValueError('Invalid requested credential assignment')
        values.append(value)
    if len(values) != 1: raise ValueError('Expected exactly one requested credential assignment')
    return values[0]


class SpendLedger:
    """Cross-process reservation before dispatch; unknown charges stay reserved."""
    def __init__(self, path, cap_usd=15):
        cap = Decimal(str(cap_usd))
        if not cap.is_finite() or not 0 < cap <= 15: raise ValueError('Shared API cap must be >0 and <=$15')
        self.cap = int((cap*1_000_000).to_integral_value(rounding=ROUND_CEILING))
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _transaction(self, change):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+') as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise HostedLimitError('Budget ledger must be an owned regular file')
            os.fchmod(file.fileno(), 0o600)
            raw = file.read()
            state = json.loads(raw) if raw else {'version': VERSION, 'cap_micro_usd': self.cap, 'reservations': []}
            if not isinstance(state, dict) or state.get('version') != VERSION or state.get('cap_micro_usd') != self.cap:
                raise HostedLimitError('Budget ledger identity/cap mismatch; do not reset it')
            rows = state.get('reservations')
            if not isinstance(rows, list) or any(not isinstance(r, dict) or
                    type(r.get('charged_micro_usd')) is not int or r['charged_micro_usd'] < 0 or
                    type(r.get('reserved_micro_usd')) is not int or r['reserved_micro_usd'] <= 0 or
                    r['charged_micro_usd'] > r['reserved_micro_usd'] or
                    r.get('state') not in ('reserved_unknown', 'completed_conservative_charge') or
                    (r['state']=='reserved_unknown' and r['charged_micro_usd']!=r['reserved_micro_usd'])
                    for r in rows):
                raise HostedLimitError('Invalid budget ledger reservations; do not reset it')
            result = change(state)
            file.seek(0); file.write(json.dumps(state, sort_keys=True)); file.truncate(); file.flush(); os.fsync(file.fileno())
            return deepcopy(result)

    def reserve(self, amount, metadata):
        if type(amount) is not int or amount <= 0: raise ValueError('Positive integer cost reservation required')
        def change(state):
            charged = sum(r['charged_micro_usd'] for r in state['reservations'])
            if charged + amount > self.cap: raise HostedLimitError(f'Shared ${self.cap/1_000_000:g} API budget exhausted; request not dispatched')
            row = {'id': uuid.uuid4().hex, 'state': 'reserved_unknown', 'reserved_micro_usd': amount,
                   'charged_micro_usd': amount, 'metadata': deepcopy(metadata)}
            state['reservations'].append(row)
            return row['id']
        return self._transaction(change)

    def settle(self, identifier, amount):
        if type(amount) is not int or amount < 0: raise ValueError('Nonnegative integer charge required')
        def change(state):
            row = next(r for r in state['reservations'] if r['id'] == identifier)
            if row['state'] != 'reserved_unknown': raise HostedLimitError('Reservation already settled')
            if amount > row['reserved_micro_usd']:
                raise HostedLimitError('Reported usage exceeded reserved ceiling; keep reservation and stop')
            row.update(state='completed_conservative_charge', charged_micro_usd=amount)
        return self._transaction(change)

    def report(self):
        def read(state):
            return {'cap_usd': self.cap/1_000_000,
                'charged_upper_bound_usd': sum(r['charged_micro_usd'] for r in state['reservations'])/1_000_000,
                'unknown_reservations': sum(r['state']=='reserved_unknown' for r in state['reservations']),
                'dispatch_reservations': len(state['reservations'])}
        return self._transaction(read)


class HostedAmplifierProvider:
    local_only = False

    def __init__(self, provider, model, *, out, budget_path=None, spend_cap_usd=15, max_calls=48, keys_path=None):
        validate_selection(provider, model)
        if type(max_calls) is not int or not 1 <= max_calls <= 48: raise ValueError('max_calls must be1..48')
        self.name, self.model, self.max_calls = provider, model, max_calls
        self.spec = deepcopy(CONFIGURATIONS[provider]); self.keys_path = keys_path
        self.out = Path(out); self.out.mkdir(parents=True, mode=0o700, exist_ok=False)
        if budget_path is None:
            from .config import default_path
            budget_path = default_path().parent/'hosted-budget.json'
        self.ledger = SpendLedger(budget_path, spend_cap_usd)
        self.records, self.budget_records, self.dispatch_records = [], [], []
        self._provider = None; self._cleanup = None; self._closed = False; self._credential = None
        self._lock = asyncio.Lock(); self._active = None
        self.metadata = {'provider': provider, 'model': model, 'version': VERSION, 'local_only': False,
            'official_module': self.spec['module'], 'provider_revision': self.spec['revision'],
            'config': deepcopy(self.spec['config']), 'max_input_tokens': MAX_INPUT, 'max_output_tokens': MAX_OUTPUT,
            'max_complete_calls': max_calls, 'max_sdk_dispatches': max_calls, 'timeout_s': 120,
            'spend_cap_usd': spend_cap_usd, 'model_id_is_weight_snapshot': False,
            'pricing_source': self.spec['pricing_source'], 'input_ceiling_per_million': self.spec['input_ceiling_per_million'],
            'output_per_million': self.spec['output_per_million'],
            'compatibility_adapter': 'instance-local nonstream SDK dispatch budget guard; official conversion/parsing unchanged'}

    async def mount(self, coordinator):
        if self._closed or self._provider is not None: raise HostedLimitError('Provider closed or already mounted')
        module = importlib.import_module(self.spec['module'])
        if importlib.metadata.version(self.spec['sdk']) != self.spec['sdk_version']:
            raise HostedLimitError('Hosted SDK version differs from frozen comparison')
        package = Path(module.__file__).parent
        source_files = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(package.rglob('*.py'))}
        if hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest() != self.spec['package_source_sha256']:
            raise HostedLimitError('Official provider sources differ from frozen revision')
        self.metadata['provider_source_files'] = source_files
        self.metadata['package_source_sha256'] = self.spec['package_source_sha256']
        config = deepcopy(self.spec['config']); self._credential = load_credential(self.spec['credential'], self.keys_path)
        config['api_key'] = self._credential
        try:
            self._cleanup = await module.mount(coordinator, config)
            self._provider = coordinator.get('providers')[self.name]
            if self._provider.use_streaming or self._provider._retry_config.max_retries != 0:
                raise HostedLimitError('Official provider retry/stream policy differs from frozen configuration')
            if self._provider.client.max_retries != 0: raise HostedLimitError('SDK retries must be disabled')
            self._install_dispatch_guard()
            self.metadata['dependencies'] = {n: importlib.metadata.version(n) for n in
                (self.spec['sdk'], 'amplifier-core', self.spec['module'].replace('_', '-'))}
            self.metadata['module_sha256'] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            _write(self.out/'provider-ready.json', self.metadata)
            await coordinator.mount('providers', self, name=self.name)
        except BaseException:
            await self.close()
            raise
        return self

    def _install_dispatch_guard(self):
        provider = self._provider
        if self.name == 'openai':
            original = provider._create_response
            async def guarded(params, *, native_input_tokens=None):
                count = native_input_tokens
                if count is None: count = await provider._native_input_token_count(params)
                return await self._dispatch(params, count, lambda: original(params, native_input_tokens=count))
            provider._create_response = guarded
        else:
            resource = provider.client.messages.with_raw_response
            original = resource.create
            async def guarded(**params):
                counted = await asyncio.wait_for(provider.client.messages.count_tokens(
                    **provider._count_tokens_params(params), timeout=15), timeout=15)
                return await self._dispatch(params, getattr(counted, 'input_tokens', None), lambda: original(**params))
            resource.create = guarded

    def _assert_safe_payload(self, payload):
        if self._credential and self._credential.encode() in _encoded(payload):
            raise HostedLimitError('Credential material detected in request/artifact payload')
        for key in payload if isinstance(payload, dict) else ():
            if key.lower() in ('api_key', 'authorization', 'x-api-key'):
                raise HostedLimitError('Authentication fields are forbidden in generation parameters')
        headers = payload.get('extra_headers', {}) if isinstance(payload, dict) else {}
        if isinstance(headers, dict) and any(k.lower() in ('authorization', 'x-api-key') for k in headers):
            raise HostedLimitError('Authentication headers are owned by the existing provider client')

    def _validate_request(self, request, request_options=None, **kwargs):
        if self._closed or self._provider is None: raise HostedLimitError('Hosted provider is not available')
        if request_options or kwargs: raise HostedLimitError('Request overrides are outside the frozen comparison')
        data = _data(request)
        self._assert_safe_payload(data)
        if data.get('model') not in (None, self.model): raise HostedLimitError('Per-request model override refused')
        if data.get('reasoning_effort') not in (None, 'medium'): raise HostedLimitError('Reasoning override refused')
        maximum = data.get('max_output_tokens')
        if maximum is not None and (type(maximum) is not int or not 1 <= maximum <= MAX_OUTPUT):
            raise HostedLimitError('Output cap exceeds fixed comparison bounds')
        if data.get('temperature') is not None or data.get('top_p') is not None or data.get('stream') is True:
            raise HostedLimitError('Sampling/stream override refused')
        return data

    async def request_budget(self, request, *, context_estimate, request_options=None):
        self._validate_request(request, request_options)
        if type(context_estimate) is not int or context_estimate < 0: raise ValueError('Nonnegative context estimate required')
        started = time.perf_counter(); sequence = len(self.budget_records)+1
        row = {'measurement': sequence, 'generation_calls': 0, 'request_sha256': _hash(request),
               'status': 'started', 'worker_measurement': {}}
        self.budget_records.append(row)
        try:
            decision = self._provider.request_budget(request, context_estimate=context_estimate)
            if inspect.isawaitable(decision): decision = await asyncio.wait_for(decision, timeout=20)
            measured = decision.get('measurement', {}) if isinstance(decision, dict) else {}
            count = measured.get('input_tokens')
            if measured.get('kind') != 'provider_count' or type(count) is not int or count < 0:
                raise HostedLimitError('Official exact input count unavailable; no generation authorized')
            result = deepcopy(decision)
            result.update(input_limit_tokens=MAX_INPUT, max_output_tokens=MAX_OUTPUT,
                context_token_budget=max(0, min(context_estimate, context_estimate+MAX_INPUT-count)))
            row.update(status='completed', worker_measurement={'input_tokens': count, 'generation_calls': 0},
                       decision=result, budget_decision=result)
            return result
        except BaseException as error:
            row.update(status='failed', error={'type': type(error).__name__})
            raise
        finally:
            row['wall_ms'] = row['service_wall_ms'] = (time.perf_counter()-started)*1000
            _write(self.out/f'budget-{sequence:03d}.json', row)

    def _charge(self, usage):
        usage = _data(usage)
        if not isinstance(usage, dict): return None
        input_count, output_count = usage.get('input_tokens'), usage.get('output_tokens')
        if type(input_count) is not int or type(output_count) is not int or min(input_count, output_count)<0: return None
        # Anthropic separates cached inputs; summing is conservative if another
        # provider already includes them in input_tokens. No cache discount assumed.
        for key in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
            value = usage.get(key)
            if value is not None:
                if type(value) is not int or value < 0: return None
                input_count += value
        cost = Decimal(input_count)*Decimal(self.spec['input_ceiling_per_million']) + Decimal(output_count)*Decimal(self.spec['output_per_million'])
        return int(cost.to_integral_value(rounding=ROUND_CEILING))

    async def _dispatch(self, params, count, invoke):
        if self._closed or self._active is None: raise HostedLimitError('Dispatch outside an active bounded call')
        self._assert_safe_payload(params)
        maximum = params.get('max_output_tokens' if self.name=='openai' else 'max_tokens')
        if params.get('model') != self.model or type(maximum) is not int or not 1 <= maximum <= MAX_OUTPUT:
            raise HostedLimitError('Automatic model/output escalation refused before SDK dispatch')
        if type(count) is not int or not 0 <= count <= MAX_INPUT: raise HostedLimitError('Full input exceeds comparison limit or exact count unavailable')
        if params.get('stream') or self._provider.use_streaming: raise HostedLimitError('Streaming bypass refused')
        if len(self.dispatch_records) >= self.max_calls: raise HostedLimitError('SDK dispatch limit exhausted')
        # Large byte-based reserve covers count uncertainty and hidden framing;
        # known usage later settles at conservative prices, never cache discounts.
        reserve_input = max(count+4096, 2*len(_encoded(params))+8192)
        reserve = self._charge({'input_tokens': reserve_input, 'output_tokens': MAX_OUTPUT})
        sequence = len(self.dispatch_records)+1
        identifier = self.ledger.reserve(reserve, {'provider': self.name, 'model': self.model,
            'run_id': str(self.out), 'dispatch': sequence})
        row = {'dispatch': sequence, 'complete_call': self._active['call'], 'reservation_id': identifier,
            'input_tokens': count, 'max_output_tokens': maximum, 'reserved_micro_usd': reserve,
            'status': 'started_unknown', 'native_request_sha256': _hash(params)}
        self.dispatch_records.append(row); self._active['dispatches'].append(row)
        self._active['inference_started'] = True
        _write(self.out/f'dispatch-{sequence:03d}-input.json', params)
        started = time.perf_counter()
        try:
            response = await invoke()
            row['status'] = 'returned'
            if self.name == 'openai':
                usage = getattr(response, 'usage', None); charge = self._charge(usage)
                if charge is not None:
                    self.ledger.settle(identifier, charge); row['charged_micro_usd'] = charge
                row['usage'] = _data(usage)
                _write(self.out/f'dispatch-{sequence:03d}-response.json', _data(response))
            return response
        except BaseException as error:
            row['error_type'] = type(error).__name__
            raise
        finally:
            row['wall_ms'] = (time.perf_counter()-started)*1000
            _write(self.out/f'dispatch-{sequence:03d}-summary.json', row)

    async def complete(self, request, *, request_options=None, **kwargs):
        async with self._lock:
            original = self._validate_request(request, request_options, **kwargs)
            if len(self.records) >= self.max_calls: raise HostedLimitError('Completion call limit exhausted')
            sequence = len(self.records)+1; started = time.perf_counter()
            row = {'call': sequence, 'request': deepcopy(original), 'inference_started': False,
                   'dispatches': [], 'complete_generation_count': None}
            self.records.append(row); self._active = row
            _write(self.out/f'call-{sequence:03d}-input.json', original)
            try:
                budget = await self.request_budget(request, context_estimate=MAX_INPUT)
                if budget['measurement']['input_tokens'] > MAX_INPUT: raise HostedLimitError('Request needs context compaction before generation')
                response = await asyncio.wait_for(self._provider.complete(request), timeout=120)
                usage = _data(response.usage)
                row['official_usage'] = deepcopy(usage)
                if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k]<0
                        for k in ('input_tokens', 'output_tokens'))):
                    raise HostedLimitError('Returned token usage unavailable or invalid; conservative reservation retained')
                if self.name == 'anthropic' and len(row['dispatches']) == 1:
                    charge = self._charge(usage)
                    if charge is not None:
                        self.ledger.settle(row['dispatches'][0]['reservation_id'], charge)
                        row['dispatches'][0]['charged_micro_usd'] = charge
                    row['dispatches'][0]['usage'] = deepcopy(usage)
                    _write(self.out/f'dispatch-{row["dispatches"][0]["dispatch"]:03d}-settlement.json', row['dispatches'][0])
                row.update(status='completed', complete_generation_count=len(row['dispatches']),
                    generation={'usage': usage, 'generation_calls': len(row['dispatches']),
                        'timing': {'generation_ms': sum(d['wall_ms'] for d in row['dispatches']),
                                   'basis': 'actual SDK dispatch wall time, includes network; not pure model compute'}})
                _write(self.out/f'call-{sequence:03d}-response.json', _data(response))
                return response
            except BaseException as error:
                row.update(status='failed', error={'type': type(error).__name__},
                    complete_generation_count=None if row['dispatches'] else 0)
                raise
            finally:
                self._active = None; row['wall_ms'] = (time.perf_counter()-started)*1000
                _write(self.out/f'call-{sequence:03d}-summary.json', row)

    def get_info(self):
        if self._provider is None: raise HostedLimitError('Mount official provider before querying info')
        info = self._provider.get_info()
        defaults = {**info.defaults, 'model': self.model, 'max_tokens': MAX_OUTPUT,
                    'max_output_tokens': MAX_OUTPUT, 'timeout': 120, 'temperature': None, 'context_window': MAX_INPUT}
        return info.model_copy(update={'defaults': defaults})

    def parse_tool_calls(self, response):
        return self._provider.parse_tool_calls(response)

    def cost_report(self):
        known = [d for d in self.dispatch_records if 'charged_micro_usd' in d]
        unknown = [d for d in self.dispatch_records if 'charged_micro_usd' not in d]
        reported_costs = [r.get('official_usage', {}).get('cost_usd') for r in self.records]
        reported_costs = [Decimal(str(c)) for c in reported_costs if c is not None]
        return {**self.ledger.report(), 'complete_calls': len(self.records),
                'sdk_dispatches': len(self.dispatch_records),
                'per_run_known_charge_upper_usd': sum(d['charged_micro_usd'] for d in known)/1_000_000,
                'per_run_unknown_reservation_count': len(unknown),
                'per_run_unknown_reserved_usd': sum(d['reserved_micro_usd'] for d in unknown)/1_000_000,
                'official_invoice_cost': None,
                'official_provider_reported_cost_estimate_usd': str(sum(reported_costs, Decimal(0))) if reported_costs else None,
                'official_provider_cost_reported_calls': len(reported_costs),
                'usage_by_dispatch': [{'dispatch': d['dispatch'], 'usage': deepcopy(d.get('usage'))} for d in self.dispatch_records],
                'accounting': 'published token-pricing conservative upper estimate, not an invoice; unknowns retain full reservation'}

    async def close(self):
        if self._closed: return
        self._closed = True
        if self._cleanup is not None: await self._cleanup()
