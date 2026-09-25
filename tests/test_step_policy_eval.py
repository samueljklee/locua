"""Step screening/oracles and actual Amplifier loop; deterministic CPU only."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import step_policy_eval as evaluation
from tests.test_continuity_eval import ScriptedProvider
from tests.test_semantic_policy_eval import fixture as legacy_fixture


def fixture(mixed=False):
    spec, oracle = legacy_fixture(mixed)
    spec['request'] = spec['request'].replace('Do not save.',
        'Automatic application persistence is acceptable; only the exact buffer is required. Do not invoke Save.')
    oracle['allowed_navigation_keys'] = []
    for goal in oracle['goals']:
        if goal['kind'] == 'text': goal['persistence_requirement'] = 'not_requested'
    return spec, oracle


def rows_from(outputs):
    rows = []
    for output in outputs:
        rows.extend(r for r in output.get('items', []) if isinstance(r, dict) and r.get('target'))
    return rows


def review_arguments(spec, oracle, rows, profile, *, wrong=False):
    by_identifier = {r.get('identifier'): r for r in rows}
    controls = {c['key']: by_identifier[c['identifier']] for c in spec['controls']
                if c['identifier'] in by_identifier}
    def outcome(key, kind, prop='value', preserve=False):
        row = controls[key]
        if isinstance(row['outcomes'], dict):
            return row['outcomes'][prop if kind == 'state' else 'value' if kind == 'display_value' else kind]
        return next(o['outcome'] for o in row['outcomes']
                    if o['type'] == kind and o.get('property', 'numeric_display' if kind == 'calculation' else 'value') == prop
                    and o.get('preserve_allowed' if preserve else 'goal_allowed', True))
    goals = []
    for goal in oracle['goals']:
        key = ('reference' if wrong and goal['key'] == 'draft' else goal['key'])
        if profile in evaluation.STEP_PROFILES:
            item = {'outcome': outcome(key, goal['kind'], goal.get('property', 'numeric_display' if goal['kind'] == 'calculation' else 'value')),
                    'value': goal.get('expression', goal.get('value'))}
            if goal['kind'] == 'text': item['persistence_requirement'] = goal['persistence_requirement']
        else:
            item = {k: deepcopy(v) for k, v in goal.items() if k not in ('key', 'evidence_plane')}
            item['target'] = controls[key]['target']
        goals.append(item)
    result = {'summary': spec['request'], 'goals': goals, 'covers_request': True}
    if profile in evaluation.STEP_PROFILES:
        result['preserves'] = [outcome(p['key'], 'state' if p['property'] != 'value' else
            ('text' if controls[p['key']]['role'] == 'AXTextField' else 'display_value'), p['property'], True)
            for p in oracle['preserves']]
        result['decision'] = {'plan': ['Inspect the requested editor and competitors', 'Apply the reviewed change', 'Verify the complete request'],
            'current_step': 'Review the observed change', 'constraints': ['Preserve the other editor', 'Do not invoke Save'],
            'unresolved': ['Fresh outcome verification remains required']}
    else:
        result['preserve'] = [{'target': controls[p['key']]['target'], 'property': p['property']} for p in oracle['preserves']]
    return result, controls


class StepScriptedProvider(ScriptedProvider):
    """Fixed CPU policy proves wiring, never local-model accuracy."""
    def __init__(self, spec, oracle, profile, *, wrong=False, falsely_done=False):
        super().__init__(spec)
        self.oracle = oracle; self.profile = profile; self.stage = 'apps'; self.rows = []
        self.wrong = wrong; self.falsely_done = falsely_done

    def next_call(self, output):
        step = self.profile in evaluation.STEP_PROFILES
        if self.stage == 'apps':
            self.stage = 'windows'; return 'locua_apps', {'query': 'Layout Lab'}
        if self.stage == 'windows':
            self.stage = 'observe'; app = output['items'][0]['app_id']
            return ('locua_inspect', {'reference': app}) if step else ('locua_windows', {'app_id': app})
        if self.stage == 'observe':
            self.stage = 'list'; self.window = output['windows'][0]['window_id']
            return ('locua_inspect', {'reference': self.window}) if step else ('locua_observe', {'window_id': self.window})
        if self.stage == 'list':
            self.view = output['view']; self.stage = 'review'
            return ('locua_search', {'reference': self.view}) if step else ('locua_inspect', {'view': self.view})
        if self.stage == 'review':
            self.rows.extend(rows_from([output]))
            continuation = output.get('coverage', {}).get('continue_with')
            if continuation: return 'locua_inspect', continuation
            args, self.controls = review_arguments(self.spec, self.oracle, self.rows, self.profile, wrong=self.wrong)
            self.stage = 'act'; return 'locua_review', args
        if self.stage == 'act':
            if output.get('status') != 'approved': return None
            self.stage = 'verify'
            if self.falsely_done:
                self.stage = 'end'
                return 'locua_status', {'decision': {'plan': ['Everything complete'], 'current_step': 'Done',
                    'constraints': [], 'unresolved': []}}
            goal = self.oracle['goals'][0]; row = self.controls[goal['key']]
            operation = 'set_text' if goal['kind'] == 'text' else 'press'
            args = {'input': row['inputs'][operation]} if step else {
                'target': row['target'], 'operation': operation}
            if operation == 'set_text': args['value'] = goal['value']
            return 'locua_act', args
        if self.stage == 'verify': self.stage = 'end'; return 'locua_verify', {}
        return None


class ActualStepLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, profile, **options):
        spec, oracle = fixture(); provider = StepScriptedProvider(spec, oracle, profile, **options)
        with tempfile.TemporaryDirectory() as temp:
            try:
                report = await evaluation.run_case(spec, oracle, Path(temp) / 'run', provider,
                    profile=profile, timeout_s=10, review_wait_s=0)
                session = evaluation.read(Path(temp) / 'run/session/session.json')
            finally: await provider.close()
        return report, provider, session

    async def test_both_real_loops_reach_exact_fresh_result_with_strict_persistence(self):
        for profile in evaluation.PROFILES:
            with self.subTest(profile=profile):
                report, provider, session = await self.run_case(profile)
                self.assertEqual(report['status'], 'passed', report)
                self.assertTrue(report['audit']['independent_post_input_ui_verified'])
                self.assertEqual(report['audit']['dispatches'], 1)
                self.assertEqual(report['persistence_contract'], 'text-persistence-v1')
                self.assertEqual(session['config']['session']['context']['config'], evaluation.CONTEXT_CONFIG)
                self.assertEqual(session['config']['session']['orchestrator']['config']['ephemeral_injection_mode'],
                    'tail' if profile in evaluation.STEP_PROFILES else 'persist')
                self.assertTrue(all(provider.spec['request'] in row['messages'][0]['content'] for row in provider.seen))

    async def test_wrong_same_named_editor_is_rejected_without_input(self):
        report, _, _ = await self.run_case('step-v1', wrong=True)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['failure_category'], 'review_semantic_mismatch')
        self.assertEqual(report['audit']['dispatches'], 0)

    async def test_model_done_claim_cannot_replace_input_or_independent_proof(self):
        report, _, session = await self.run_case('step-v1', falsely_done=True)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['outcome_achieved'])
        self.assertEqual(report['audit']['dispatches'], 0)
        self.assertNotIn('verified_completion', session)


class InformedDecisionTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_arithmetic_mapping_uses_pure_existing_witness_and_detects_wrong_digit(self):
        from locua.arithmetic_input import symbol
        spec, oracle = evaluation.sem.read_cases(ROOT / 'artifacts/step-scope-v16-001/evaluator-audit/frozen-cases')[1][0]
        for profile in evaluation.PROFILES:
            for wrong in (False, True):
                with self.subTest(profile=profile, wrong=wrong), tempfile.TemporaryDirectory() as tmp:
                    owner = evaluation.owner_for(spec, Path(tmp) / 'owner', profile)
                    try:
                        outputs = evaluation.informed_setup(owner); rows = rows_from(outputs)
                        evaluation.seed_calculation_review(owner, spec, oracle, outputs)
                        by_symbol = {symbol({'role': row['role'], 'name': row.get('name')}): row for row in rows
                                     if symbol({'role': row['role'], 'name': row.get('name')}) is not None}
                        expression = oracle['goals'][0]['expression']
                        if wrong: expression = expression.replace('9', '0', 1)
                        selected = [by_symbol[token] for token in ['clear', *expression, '=']]
                        args = {'inputs': [row['inputs']['press'] for row in selected]} if profile in evaluation.STEP_PROFILES else {
                            'steps': [{'target': row['target'], 'operation': 'press'} for row in selected]}
                        score = evaluation.score_action(owner, {'name': 'locua_act_sequence', 'arguments': args}, spec, oracle)
                        self.assertEqual(score['decision_acceptable'], not wrong, score)
                        self.assertTrue(score['actual_witness_unchanged']); self.assertFalse(owner.desktop.executions)
                        self.assertFalse(score['independent_display_result_verified'])
                    finally: owner.close()

    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_arithmetic_optional_press_claims_do_not_require_complete_inventory(self):
        spec, oracle = evaluation.sem.read_cases(ROOT / 'artifacts/step-scope-v16-001/evaluator-audit/frozen-cases')[1][0]
        with tempfile.TemporaryDirectory() as tmp:
            owner = evaluation.owner_for(spec, Path(tmp) / 'owner', 'semantic-v1')
            try:
                outputs = evaluation.informed_setup(owner); rows = rows_from(outputs)
                evaluation.seed_calculation_review(owner, spec, oracle, outputs)
                review = evaluation.read(next(owner.out.glob('review-*.json')))
                observation = owner._observation(review['review_capture']['snapshot_id'])
                from locua.engine.prototype.core import _identity
                control = next(c for c in observation['controls'] if c.get('name') == '2')
                review['effects'].append({'kind': 'press', 'control_id': control['id'], 'identity': _identity(control, observation), 'purpose': 'Enter 2'})
                gate = evaluation.review_gate(review, spec, oracle, live_observation=observation)
                self.assertTrue(gate['structured_pass'], gate)
                self.assertFalse(gate['accepted']); self.assertEqual(gate['decision'], 'needs_independent_prose_review')
                review['effects'][-1]['purpose'] = 'Enter 9'
                gate = evaluation.review_gate(review, spec, oracle, live_observation=observation)
                self.assertFalse(gate['structured_pass']); self.assertIn('explicit_digit_purpose_contradicts_observed_control', gate['reasons'])
            finally: owner.close()


class ArithmeticPurposeRegressionTests(unittest.TestCase):
    def test_eight_anchored_and_whole_plan_purpose_cases(self):
        cases=[('Enter 9','9'),('Enter digit 9.','9'),('digit 9','9'),('Press 9 button','9'),
            ('Enter 1,9,2,×,2,3,1,−,1,0,0 and Equals.',None),
            ('Enter 192 then multiply.',None),('Enter 9; press 2 next.',None),
            ('Evaluate the expression, then enter 1,9,2.',None)]
        for text,wanted in cases:
            with self.subTest(text=text):self.assertEqual(evaluation.single_digit_purpose(text),wanted)

    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_actual_phase004_whole_plan_is_not_single_digit_contradiction_but_prose_still_blocks(self):
        root=ROOT/'artifacts/step-scope-v16-001/qwen38-step2-arithmetic-warm-004/case/tools'
        review=evaluation.read(root/'review-1.json')
        observation=evaluation.read(next(root.glob('observation-*.json')))
        spec,oracle=next((s,o) for s,o in evaluation.sem.read_cases(ROOT/'artifacts/step-scope-v16-001/evaluator-audit/frozen-cases')[1]
            if s['id']=='arithmetic-development')
        gate=evaluation.review_gate(review,spec,oracle,live_observation=observation)
        self.assertTrue(gate['structured_pass'],gate)
        self.assertNotIn('explicit_digit_purpose_contradicts_observed_control',gate['reasons'])
        self.assertFalse(gate['accepted']);self.assertEqual(gate['decision'],'needs_independent_prose_review')
        self.assertIn('43,452',review['summary'])
        negative={'review_sha256':evaluation.base.digest(review),'request_sha256':evaluation.base.digest(spec['request']),
            'approved':False,'contradictory_claims':True,'reviewer':'independent-cpu-audit',
            'reason':'User-facing summary predicts an incorrect arithmetic result; no faithful prose approval.'}
        self.assertFalse(evaluation.review_gate(review,spec,oracle,live_observation=observation,
            prose_attestation=negative)['accepted'])
        # A stale judgment or a truthful disclosure of contradictory claims
        # cannot act as approval, even if its approved flag is forged true.
        negative['approved']=True
        self.assertFalse(evaluation.review_gate(review,spec,oracle,live_observation=observation,
            prose_attestation=negative)['accepted'])

    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_unobserved_control_still_rejected_despite_whole_plan_purpose(self):
        root=ROOT/'artifacts/step-scope-v16-001/qwen38-step2-arithmetic-warm-004/case/tools'
        review=evaluation.read(root/'review-1.json');observation=evaluation.read(next(root.glob('observation-*.json')))
        spec,oracle=next((s,o) for s,o in evaluation.sem.read_cases(ROOT/'artifacts/step-scope-v16-001/evaluator-audit/frozen-cases')[1]
            if s['id']=='arithmetic-development')
        next(e for e in review['effects'] if e['kind']=='press')['control_id']='invented'
        gate=evaluation.review_gate(review,spec,oracle,live_observation=observation)
        self.assertFalse(gate['structured_pass']);self.assertIn('unobserved_or_nonarithmetic_press_claim',gate['reasons'])


class FrozenReconstructionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cases = ROOT / 'artifacts/step-scope-v16-001/evaluator-audit/frozen-cases'
        manifest, rows = evaluation.sem.read_cases(cases)
        self.spec, self.oracle = next(row for row in rows if row[0]['id'] == 'arithmetic-development')
        with patch.object(evaluation.sem, 'read_cases', return_value=(manifest, [(self.spec, self.oracle)])), \
             patch.object(evaluation, 'configuration_identity', return_value={'path': 'cpu-only', 'sha256': 'test'}):
            self.phase = evaluation.prepare(cases, self.root / 'phase')

    def rendering(self, profile, action=False):
        record = self.phase['renderings'][self.spec['id'] + '--' + profile + ('--action' if action else '')]
        return evaluation.read(self.root / 'phase' / record['path'])

    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    async def test_both_profiles_review_and_action_reconstruct_exactly_after_delay(self):
        from locua.arithmetic_input import symbol
        for profile in evaluation.PROFILES:
            for action in (False, True):
                with self.subTest(profile=profile, action=action):
                    rendering = self.rendering(profile, action)
                    outputs = json.loads(rendering['request']['messages'][2]['content'].split('\n', 1)[1])
                    rows = rows_from(outputs)
                    if action:
                        by_symbol = {symbol({'role': row['role'], 'name': row.get('name')}): row for row in rows
                            if symbol({'role': row['role'], 'name': row.get('name')}) is not None}
                        selected = [by_symbol[token] for token in ['clear', *self.oracle['goals'][0]['expression'], '=']]
                        args = {'inputs': [r['inputs']['press'] for r in selected]} if profile in evaluation.STEP_PROFILES else {
                            'steps': [{'target': r['target'], 'operation': 'press'} for r in selected]}
                        name = 'locua_act_sequence'
                    else:
                        args, _ = review_arguments(self.spec, self.oracle, rows, profile)
                        name = 'locua_review'
                    provider = ScriptedProvider(self.spec)
                    provider.next_call = lambda _output: (name, args)
                    actual_complete = provider.complete
                    after_delay_ns = rendering['fixture_clock']['setup_clock_ns'] + 61_000_000_000
                    async def complete(request):
                        self.assertEqual(evaluation.time.time_ns(), after_delay_ns,
                                         'Frozen setup clock leaked into provider generation')
                        return await actual_complete(request)
                    provider.complete = complete
                    try:
                        with patch('time.time_ns', return_value=after_delay_ns):
                            report = await evaluation.run_isolated(self.spec, self.oracle,
                                self.root / (profile + str(action)), provider, profile=profile,
                                rendering=rendering, action_mode=action, timeout_s=5)
                        self.assertEqual(report['status'], 'passed' if action else 'pending_review', report)
                        if not action: self.assertTrue(report['decision']['structured_semantics_pass'])
                        self.assertTrue(report['provider_started'])
                        self.assertEqual(report['provider_attempts'], 1)
                        self.assertEqual(report['application_inputs'], 0)
                        self.assertFalse(report['task_completed'])
                        if action: self.assertTrue(report['decision']['actual_witness_unchanged'])
                    finally: await provider.close()

    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    async def test_corrupt_private_cursor_fails_preflight_without_provider(self):
        rendering = self.rendering('semantic-v1')
        rendering['reference_map']['p1'] = ['corrupt-private-page']
        provider = ScriptedProvider(self.spec)
        try:
            report = await evaluation.run_isolated(self.spec, self.oracle, self.root / 'corrupt',
                provider, profile='semantic-v1', rendering=rendering, timeout_s=5)
            self.assertEqual(report['failure_category'], 'evaluator_preflight', report)
            self.assertFalse(report['provider_started']); self.assertEqual(report['provider_attempts'], 0)
            self.assertEqual(report['application_inputs'], 0)
        finally: await provider.close()


class WarmDecisionProvider(StepScriptedProvider):
    def __init__(self,*args,mode='execute',**kwargs):
        super().__init__(*args,**kwargs);self.mode=mode;self.real_index=0
    async def complete(self,request,**kwargs):
        from locua.amplifier_provider import native_request
        if not self.seen:
            native=native_request(request,model=self.model,structured_tool_results=self._structured_tool_results)
            for message in native['messages']:
                if message['role']!='tool':continue
                output=json.loads(message['content'])['output']
                if isinstance(output,str):output=json.loads(output)
                if 'output' in output:output=output['output']
                self.rows.extend(rows_from([output]))
            self.stage='review'
        return await super().complete(request,**kwargs)
    def next_call(self,output):
        if self.mode=='plain':return None
        if self.mode=='discover':
            rows=list({r['target']:r for r in self.rows}.values());row=rows[self.real_index%len(rows)]
            self.real_index+=1
            return ('locua_inspect',{'reference':row['target']}) if self.profile in evaluation.STEP_PROFILES else (
                'locua_inspect',{'view':'v1','target':row['target']})
        if self.stage=='act' and self.oracle['goals'][0]['kind']=='calculation':
            if output.get('status')!='approved':return None
            from locua.arithmetic_input import symbol
            by_symbol={symbol({'role':r['role'],'name':r.get('name')}):r for r in self.rows
                if symbol({'role':r['role'],'name':r.get('name')}) is not None}
            expression=self.oracle['goals'][0]['expression']
            if self.mode=='wrong_mapping':expression=expression.replace('9','0',1)
            selected=[by_symbol[token] for token in ['clear',*expression,'=']]
            self.stage='end'
            return 'locua_act_sequence',({'inputs':[r['inputs']['press'] for r in selected]} if self.profile in evaluation.STEP_PROFILES else {
                'steps':[{'target':r['target'],'operation':'press'} for r in selected]})
        return super().next_call(output)


class WarmLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_warm(self,spec,oracle,profile,mode='execute'):
        provider=WarmDecisionProvider(spec,oracle,profile,mode=mode)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                report=await evaluation.run_case(spec,oracle,Path(tmp)/'run',provider,profile=profile,
                    warm_start=True,decision_limit=4,max_calls=18,timeout_s=10,review_wait_s=0)
                first=evaluation.read(Path(tmp)/'run/warm-start/first-real-provider-request.json')
            finally:await provider.close()
        return report,first,provider

    async def test_same_actual_tool_receipts_then_editor_review_act_and_independent_verify(self):
        spec,oracle=fixture()
        for profile in evaluation.PROFILES:
            with self.subTest(profile=profile):
                report,first,provider=await self.run_warm(spec,oracle,profile)
                self.assertEqual(report['status'],'passed',report)
                self.assertTrue(report['outcome_achieved']);self.assertEqual(report['audit']['dispatches'],1)
                self.assertEqual(report['real_model_decision_requests'],3)
                self.assertTrue(report['evaluator_read_only_discovery_assistance'])
                self.assertEqual(report['focus_dedup_requested'],profile in evaluation.STEP_PROFILES)
                self.assertEqual(report['focus_dedup_enabled'],profile in evaluation.STEP_PROFILES)
                if profile=='semantic-v1':self.assertEqual(report['focus_context_events'],[])
                self.assertFalse(report['model_started_from_user_request_alone'])
                self.assertEqual(sum(m['role']=='tool' for m in first['messages']),4)
                self.assertIn(spec['request'],first['messages'][0]['content'])
                self.assertNotIn('Evaluator read-only setup:',json.dumps(first))

    async def test_additional_valid_discovery_is_budget_inconclusive_not_model_failure(self):
        spec,oracle=fixture()
        report,_,provider=await self.run_warm(spec,oracle,'step-v1','discover')
        self.assertEqual(report['status'],'inconclusive',report)
        self.assertEqual(report['failure_category'],'decision_budget_inconclusive')
        self.assertEqual(len(provider.records),4);self.assertEqual(report['audit']['dispatches'],0)

    async def test_normal_prose_without_task_execution_is_informed_nonprogress(self):
        spec,oracle=fixture()
        report,_,_=await self.run_warm(spec,oracle,'step-v1','plain')
        self.assertEqual(report['failure_category'],'no_tool_progress',report)
        self.assertEqual(report['audit']['dispatches'],0)

    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    async def test_arithmetic_pure_mapping_never_claims_display_completion(self):
        cases=ROOT/'artifacts/step-scope-v16-001/evaluator-audit/frozen-cases'
        spec,oracle=next(row for row in evaluation.sem.read_cases(cases)[1] if row[0]['id']=='arithmetic-development')
        original=evaluation.review_gate
        def cpu_independent_attestation(review,spec,oracle,**kwargs):
            gate=original(review,spec,oracle,**kwargs)
            if gate['structured_pass'] and not gate['accepted']:
                kwargs['prose_attestation']={'review_sha256':evaluation.base.digest(review),
                    'request_sha256':evaluation.base.digest(spec['request']),'approved':True,
                    'contradictory_claims':False,'reviewer':'CPU known test review',
                    'reason':'CPU expected original expression and observed current readout, no action dispatch'}
                return original(review,spec,oracle,**kwargs)
            return gate
        for profile in evaluation.PROFILES:
            for mode in ('execute','wrong_mapping'):
                with self.subTest(profile=profile,mode=mode),patch.object(evaluation,'review_gate',cpu_independent_attestation):
                    report,_,_=await self.run_warm(spec,oracle,profile,mode)
                    self.assertEqual(report['arithmetic_mapping_verified'],mode=='execute',report)
                    self.assertFalse(report['outcome_achieved']);self.assertFalse(report['independent_display_result_verified'])
                    self.assertEqual(report['audit']['dispatches'],0)
                    self.assertTrue(all(r.get('actual_witness_unchanged',True) for r in report['mapping_receipts']))

    def test_measured_context_source_pins_are_explicit_and_candidate_only(self):
        identity=evaluation.focus_context_identity()
        self.assertEqual(identity['enabled_by_profile'],{'semantic-v1':False,'step-v1':True,'step-v2':True})
        self.assertEqual(identity['adapter_source_sha256'],evaluation.sha(ROOT/'src/locua/focus_context.py'))
        self.assertEqual(identity['session_source_sha256'],evaluation.sha(ROOT/'src/locua/amplifier_session.py'))
        self.assertTrue(identity['compatible'])
        self.assertEqual(identity['observed_upstream']['source_hashes'],identity['upstream_source_pins'])
        before={'context':evaluation.CONTEXT_CONFIG}
        after={**before,'focus_context':identity}
        delta=evaluation.causal_reset(before,after,['context'])
        self.assertEqual(delta['declared_changes'],['context'])
        self.assertEqual(evaluation.lane_regime(before,'baseline','semantic-v1'),evaluation.lane_regime(after,'baseline','semantic-v1'))
        self.assertNotEqual(evaluation.lane_regime(before,'baseline','step-v1'),evaluation.lane_regime(after,'baseline','step-v1'))

    def test_length_classification_and_causal_resets(self):
        class Provider:records=[{'generation':{'finish_reason':'length'}}]
        self.assertEqual(evaluation.outcome_category({'failure_category':'provider_availability_or_time_budget'},Provider()),'output_budget_exhausted')
        phase={'message_construction':'user_bundle'}
        with self.assertRaisesRegex(ValueError,'causal'):evaluation.causal_reset(phase,dict(phase),[])
        changed={**phase,'message_construction':'actual_tool_pairs'}
        with self.assertRaisesRegex(ValueError,'causal'):evaluation.causal_reset(phase,changed,[])
        result=evaluation.causal_reset(phase,changed,['message_construction'])
        self.assertEqual(result['declared_changes'],['message_construction'])


class FreezeAndExposureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.phase_path = self.root / 'phase'; self.phase_path.mkdir()
        (self.phase_path / 'phase.json').write_text('{"source":"frozen"}')
        self.ledger = self.root / 'ledger.json'; self.ledger.write_text('{"events":[]}')
        self.phase = {'exposure_ledger': str(self.ledger), 'cases': ['arithmetic-development','appearance-development','editor-development']}
        self.spec = {'id': 'arithmetic-development', 'split': 'development'}

    def record(self, report=None, model='qwen38', profile='step-v1', mode='isolated'):
        return evaluation.exposure(self.phase_path, self.phase, model, profile, self.spec, mode, report=report)

    def test_no_duplicate_run_and_pending_prose_can_be_audited_without_new_start(self):
        self.record(); self.record({'status':'pending_review','failure_category':'review_audit_pending'})
        self.record({'status':'passed','failure_category':'informed_review_verified'})
        with self.assertRaisesRegex(ValueError, 'already attempted'): self.record()
        events = evaluation.read(self.ledger)['events']
        self.assertEqual(sum(e['event']=='started' for e in events), 1)
        self.assertEqual(events[-1]['status'], 'passed')

    def test_two_equivalent_informed_failures_stop_lane(self):
        for cid in ('first-development', 'second-development'):
            self.spec['id'] = cid; self.record(); self.record({'status':'failed','failure_category':'argument_contract'})
        self.spec['id'] = 'third-development'
        with self.assertRaisesRegex(ValueError, 'two equivalent'): self.record()

    def test_no_tool_failures_stop_even_after_new_phase_label(self):
        for cid in ('first-development','second-development'):
            self.spec['id']=cid;self.record();self.record({'status':'failed','failure_category':'no_tool_progress'})
        (self.phase_path/'phase.json').write_text('{"new_phase_name":"same_causal_inputs"}')
        self.spec['id']='third-development'
        with self.assertRaisesRegex(ValueError,'two equivalent'):self.record()

    def test_unrelated_candidate_edit_does_not_reset_baseline_lane(self):
        before={'tool_specs':{'semantic-v1':[],'step-v1':[]},'source_hashes':{'src/locua/step_interface.py':'old'}}
        after=deepcopy(before);after['source_hashes']['src/locua/step_interface.py']='new'
        self.assertEqual(evaluation.lane_regime(before,'baseline','semantic-v1'),evaluation.lane_regime(after,'baseline','semantic-v1'))
        self.assertNotEqual(evaluation.lane_regime(before,'baseline','step-v1'),evaluation.lane_regime(after,'baseline','step-v1'))

    def test_heldout_warm_gate_needs_mapping_and_independent_completion(self):
        heldout={'id':'choice-heldout','split':'heldout'}
        with self.assertRaisesRegex(ValueError,'mapping'):evaluation.promotion_check(self.phase_path,self.phase,'qwen38',heldout,'warm-loop')
        for cid in self.phase['cases']:
            self.spec['id']=cid;self.record(mode='warm-loop')
            self.record({'status':'inconclusive','failure_category':'safe_discovery_only'},mode='warm-loop')
        with self.assertRaisesRegex(ValueError,'mapping'):evaluation.promotion_check(self.phase_path,self.phase,'qwen38',heldout,'warm-loop')
        for cid in self.phase['cases']:
            self.spec['id']=cid
            self.record({'status':'passed','failure_category':'component_verified',
                'arithmetic_mapping_verified':cid.startswith('arithmetic-'),
                'outcome_achieved':not cid.startswith('arithmetic-'),'audit':{'passed':True},
                'normal_session_completion':True},mode='warm-loop')
        evaluation.promotion_check(self.phase_path,self.phase,'qwen38',heldout,'warm-loop')

    def test_one_global_heldout_exposure_across_phases(self):
        self.spec = {'id':'choice-heldout','split':'heldout'}; self.record()
        self.record(model='comparator',profile='semantic-v1')
        (self.phase_path / 'phase.json').write_text('{"source":"retuned"}')
        with self.assertRaisesRegex(ValueError, 'globally fresh'): self.record(model='baseline')

    def test_heldout_gate_is_per_model_and_requires_all_three_informed_decisions(self):
        heldout = {'id':'choice-heldout','split':'heldout'}
        with self.assertRaisesRegex(ValueError, 'all three'): evaluation.promotion_check(self.phase_path,self.phase,'qwen38',heldout,'isolated')
        for cid in self.phase['cases']:
            self.spec['id']=cid; self.record(); self.record({'status':'passed','failure_category':'informed_review_verified'})
        evaluation.promotion_check(self.phase_path,self.phase,'qwen38',heldout,'isolated')
        with self.assertRaisesRegex(ValueError, 'all three'): evaluation.promotion_check(self.phase_path,self.phase,'comparator',heldout,'isolated')

    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_frozen_development_cases_render_only_visible_data_and_original_goal(self):
        root = ROOT / 'artifacts/step-scope-v16-001/evaluator-audit/frozen-cases'
        for spec, oracle in evaluation.sem.read_cases(root)[1]:
            if spec['split'] != 'development': continue
            for profile in evaluation.PROFILES:
                with self.subTest(case=spec['id'], profile=profile), tempfile.TemporaryDirectory() as tmp:
                    owner = evaluation.owner_for(spec, Path(tmp) / 'owner', profile)
                    try:
                        output = evaluation.informed_setup(owner); payload = evaluation.request_render(owner, output)
                        raw = json.dumps(payload, ensure_ascii=False)
                        self.assertIn(spec['request'], payload['messages'][0]['content'])
                        for key in ('expected_state', 'independent_expected_numeric_result', 'faithful_goal_key'):
                            self.assertNotIn(key, raw)
                        if 'independent_expected_numeric_result' in oracle:
                            self.assertNotIn(str(oracle['independent_expected_numeric_result']), raw)
                    finally: owner.close()

    def test_actual_review_compilation_scores_typed_value_preservation_and_persistence(self):
        spec, oracle = fixture()
        for profile in evaluation.PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                owner = evaluation.owner_for(spec, Path(tmp) / 'owner', profile)
                try:
                    outputs = evaluation.informed_setup(owner)
                    args, _ = review_arguments(spec, oracle, rows_from(outputs), profile)
                    score = evaluation.score_isolated(owner, {'name': 'locua_review', 'arguments': args}, spec, oracle)
                    self.assertTrue(score['decision_acceptable'], score)
                    self.assertFalse(owner.desktop.executions)
                finally: owner.close()

    def test_unknown_prose_stays_pending_and_safe_discovery_is_not_success(self):
        spec, oracle = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            owner = evaluation.owner_for(spec, Path(tmp) / 'owner', 'step-v1')
            try:
                outputs = evaluation.informed_setup(owner)
                args, _ = review_arguments(spec, oracle, rows_from(outputs), 'step-v1')
                args['summary'] = 'Change the editor.'
                score = evaluation.score_isolated(owner, {'name': 'locua_review', 'arguments': args}, spec, oracle)
                self.assertFalse(score['decision_acceptable']); self.assertEqual(score['category'], 'review_audit_pending')
            finally: owner.close()
        with tempfile.TemporaryDirectory() as tmp:
            owner = evaluation.owner_for(spec, Path(tmp) / 'owner', 'step-v1')
            try:
                evaluation.informed_setup(owner)
                score = evaluation.score_isolated(owner, {'name': 'locua_status', 'arguments': {}}, spec, oracle)
                self.assertFalse(score['decision_acceptable']); self.assertTrue(score['safe_discovery'])
            finally: owner.close()


class StepV2FreezeTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'artifacts/step-scope-v16-001').is_dir(),
                         'Private retained replay evidence is not distributed')
    def test_phase_regime_is_pure_and_stable_across_serialized_roundtrips(self):
        phase = evaluation.read(ROOT / 'artifacts/step-scope-v16-001/frozen-phase-003/phase.json')
        original = deepcopy(phase)
        for profile in ('semantic-v1', 'step-v1'):
            first = evaluation.lane_regime(phase, 'baseline', profile)
            self.assertEqual(evaluation.lane_regime(phase, 'baseline', profile), first)
            self.assertEqual(evaluation.lane_regime(json.loads(json.dumps(phase)), 'baseline', profile), first)
            self.assertEqual(phase, original)
        self.assertEqual(evaluation.base.digest(evaluation.causal_factors(phase)), phase['causal_regime'])

    def test_explicit_initial_cohort_does_not_restart_other_models_or_profiles(self):
        cohort = evaluation.parse_allowed_trials(['qwen38/step-v2/arithmetic-development/warm-loop'],
            {'arithmetic-development', 'editor-development', 'appearance-heldout'})
        phase = {'allowed_trials': cohort}
        evaluation.cohort_check(phase, 'qwen38', 'step-v2', 'arithmetic-development', 'warm-loop')
        for model, profile, case, mode in [
            ('baseline','step-v2','arithmetic-development','warm-loop'),
            ('comparator','step-v2','arithmetic-development','warm-loop'),
            ('qwen38','step-v1','arithmetic-development','warm-loop'),
            ('qwen38','step-v2','editor-development','warm-loop'),
            ('qwen38','step-v2','appearance-heldout','warm-loop'),
            ('qwen38','step-v2','arithmetic-development','warm-loop-operator-review-makeup-once')]:
            with self.assertRaisesRegex(ValueError, 'initial cohort'):
                evaluation.cohort_check(phase,model,profile,case,mode)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            evaluation.parse_allowed_trials(['qwen38/step-v2/arithmetic-development/warm-loop']*2,{'arithmetic-development'})

    def test_step_regime_stop_survives_start_finish_and_new_process_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = root/'ledger.json'; ledger.write_text('{"events": []}')
            phase = {'cases': ['arithmetic-development','appearance-development','editor-development'],
                'exposure_ledger': str(ledger), 'tool_specs': {'step-v2': []},
                'focus_context': {'enabled_by_profile': {'step-v2': True},'adapter_source_sha256': 'fixed'},
                'source_hashes': {},'candidate_reminder_mode': 'tail'}
            (root/'phase.json').write_text(json.dumps(phase))
            for case in phase['cases'][:2]:
                spec = {'id': case,'split': 'development'}
                current = evaluation.read(root/'phase.json')
                evaluation.exposure(root,current,'baseline','step-v2',spec,'warm-loop')
                evaluation.exposure(root,current,'baseline','step-v2',spec,'warm-loop',report={
                    'status':'failed','failure_category':'no_tool_progress'})
            events = evaluation.read(ledger)['events']
            self.assertEqual(len({e['causal_regime'] for e in events}),1)
            with self.assertRaisesRegex(ValueError,'two equivalent'):
                evaluation.exposure(root,evaluation.read(root/'phase.json'),'baseline','step-v2',
                    {'id':'editor-development','split':'development'},'warm-loop')

    def test_followup_cohort_requires_same_phase_model_profile_mapping_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); ledger=root/'ledger.json'; ledger.write_text('{"events":[]}')
            phase={'mapping_prerequisite':'arithmetic-development','exposure_ledger':str(ledger),
                'allowed_trials':evaluation.parse_allowed_trials([
                    'qwen38/step-v2/arithmetic-development/warm-loop',
                    'qwen38/step-v2/editor-development/warm-loop'],
                    {'arithmetic-development','editor-development'})}
            (root/'phase.json').write_text(json.dumps(phase)); digest=evaluation.sha(root/'phase.json')
            evaluation.cohort_check(phase,'qwen38','step-v2','arithmetic-development','warm-loop',root)
            row={'event':'finished','phase_sha256':digest,'model':'qwen38','profile':'step-v2',
                'mode':'warm-loop','case':'arithmetic-development','verified_arithmetic_mapping':True}
            for changed in ({'verified_arithmetic_mapping':False},{'profile':'step-v1'},
                            {'model':'comparator'},{'phase_sha256':'earlier'},
                            {'mode':'warm-loop-operator-review-makeup-once'}):
                ledger.write_text(json.dumps({'events':[{**row,**changed}]}))
                with self.assertRaisesRegex(ValueError,'mapping pass'):
                    evaluation.cohort_check(phase,'qwen38','step-v2','editor-development','warm-loop',root)
            ledger.write_text(json.dumps({'events':[row]}))
            evaluation.cohort_check(phase,'qwen38','step-v2','editor-development','warm-loop',root)

    def test_new_candidate_heldout_gate_does_not_inherit_old_candidate_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); phase={'candidate_profile':'step-v2','cases':['arithmetic-development','editor-development','appearance-development'],
                'exposure_ledger':str(root/'ledger.json')}
            (root/'phase.json').write_text(json.dumps(phase)); digest=evaluation.sha(root/'phase.json')
            events=[{'event':'finished','phase_sha256':digest,'model':'qwen38','profile':'step-v1','mode':'warm-loop',
                'case':case,'verified_arithmetic_mapping':True,'verified_synthetic_completion':True} for case in phase['cases']]
            (root/'ledger.json').write_text(json.dumps({'events':events}))
            with self.assertRaisesRegex(ValueError,'mapping'):
                evaluation.promotion_check(root,phase,'qwen38',{'id':'appearance-heldout','split':'heldout'},'warm-loop')


if __name__ == '__main__': unittest.main()
