"""Readable progress over the existing reviewed plan and verification records.

No second plan store, action policy, input authority or language interpretation.
Only the predicates actually reviewed by the user can receive completed status.
"""
from .amplifier_tools import _hash


def _proof(owner, scope_id, scope):
    result = {'matched': False, 'reason': 'Fresh verification of the reviewed predicates is still required.'}
    if owner._cancellation is not None or scope['status'] not in ('approved', 'reconciled_verified'):
        return {**result, 'reason': 'Reviewed scope is blocked or canceled.'}
    if any(s.get('uncertain_action') and s['target'] == scope['target'] for s in owner._scopes.values()):
        return {**result, 'reason': 'Uncertain input requires read-only reconciliation.'}
    if owner.evidence.get('unmet_persistence_requirements'):
        return {**result, 'reason': 'A declared persistence requirement remains unproved.'}
    receipt = owner.evidence.get('verification', {}).get(scope_id)
    latest = owner._latest.get(_hash(scope['target']))
    if not receipt or latest is None or not scope['goals']:
        return result
    wanted = {g['id']: g for g in scope['goals']}
    actual = {r['goal_id']: r for r in receipt.get('goals', [])}
    if set(actual) != set(wanted):
        return {**result, 'reason': 'Not every reviewed goal has a verification result.'}
    preserved = receipt.get('preserves', [])
    if ({r.get('predicate_id') for r in preserved} != {p['goal']['id'] for p in scope['preserves']}
            or not all(r.get('matched') is True and (r.get('evidence') or {}).get('snapshot_id') == latest for r in preserved)):
        return {**result, 'reason': 'A preserved property is changed or unverified.'}
    checks = []
    for gid, goal in wanted.items():
        row = actual[gid]
        evidence = row.get('evidence') or {}
        matched = row.get('matched') is True and evidence.get('snapshot_id') == latest
        if goal['kind'] == 'calculation':
            matched = matched and scope['witness'].matches(goal['expression'])
        checks.append({'goal': gid, 'matched': bool(matched), 'reason': row.get('reason'),
                       'snapshot_id': evidence.get('snapshot_id'), 'plane': evidence.get('plane')})
    matched = (all(r['matched'] for r in checks)
        and receipt.get('all_preservation_predicates_matched') is True
        and receipt.get('all_reviewed_predicates_matched') is True)
    return {'matched': matched, 'goals': checks, 'snapshot_id': latest,
        'reason': 'All reviewed predicates matched at the latest retained capture.' if matched
                  else 'A reviewed outcome or its required input-issuance evidence is incomplete.'}


def reviewed_plan_progress(owner, review_reference):
    """Project canonical scopes; caller holds the existing owner lock.

    A review already contains the plan, exact goals and constraints. A model's
    optional planning prose remains advisory, not an extra completion criterion.
    Discovery and goal-less navigation do not create artificial final outcomes.
    """
    active = None
    prior_proof = set()
    for event in owner.evidence.get('events', []):
        result = event.get('result') or {}
        args = event.get('input') or {}
        sid = args.get('scope_id') or result.get('scope_id')
        if event.get('tool') in ('locua_review', 'locua_act', 'locua_act_sequence') and sid in owner._scopes:
            active = sid
        if event.get('tool') == 'locua_verify':
            prior_proof.update(key for key, row in result.get('scopes', {}).items()
                               if row.get('all_reviewed_predicates_matched') is True)
    items = []
    navigation = []
    for sid, scope in owner._scopes.items():
        ref = review_reference(sid)
        if not scope['goals']:
            navigation.append(ref)
            continue
        proof = _proof(owner, sid, scope)
        complete = proof['matched']
        items.append({'review': ref,
            'status': 'completed' if complete else 'current' if sid == active else 'pending',
            'completion_meaning': 'Only the explicitly reviewed predicates, not model-written plan prose.',
            'criteria': {'goals': [{'goal': g['id'], 'kind': g['kind'], 'plane': g.get('evidence_plane')}
                                  for g in scope['goals']],
                         'preserves': len(scope['preserves']),
                         'exact_criteria': {'tool': 'locua_status', 'review': ref}},
            'verification': proof,
            'requires_recheck': sid in prior_proof and not complete,
            'whole_request_coverage_reviewed': scope['covers_entire_request'],
            'unresolved_requirements': list(scope['unresolved_requirements'])})
    active_ref = review_reference(active) if active is not None else None
    if not any(row['review'] == active_ref and row['status'] == 'current' for row in items):
        active_ref = None
    return {'items': items, 'active_review': active_ref, 'navigation_reviews': navigation,
        'status_authority': 'canonical_reviewed_goals_and_existing_owner_verification',
        'model_plan_prose_coverage_proven': False,
        'retained_evidence_may_be_stale': True, 'fresh_readback_performed_now': False,
        'action_authority': False, 'task_complete': False,
        'completion_limit': 'Independent language coverage and final task verification are separate.'}
