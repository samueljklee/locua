"""Read-only continuation views after input; no task or action policy.

Region correspondence is a discovery convenience, never action identity. The
ordinary freshness/identity/scope guards must still validate every next input.
"""
from . import progressive_ui
from .engine.prototype.regions import catalog_regions


def post_action_views(before, acted_control_id, after, actions):
    """Two bounded, independently paged views of the actual post-action capture.

Keep every regional competitor and global discovery. A changed/ambiguous region
falls back to overview without guessing an ordinal, coordinate or task target.
"""
    sid = after['snapshot_id']
    result = {'continuation_policy': 'fresh-region-v1',
              'continuation_is_action_authority': False}
    try:
        result['overview'] = progressive_ui.overview(after, actions=actions, max_bytes=3072)
    except ValueError as error:
        result['overview'] = {'status': 'unavailable', 'reason': str(error),
            'snapshot_id': sid, 'next': 'locua_inspect overview or list; evidence remains retained.'}
    try:
        if before['target'] != after['target']:
            raise ValueError('Post-action target changed; no region correspondence inferred')
        old, new = catalog_regions(before), catalog_regions(after)
        prior = next(r for r in old['regions'] if r['id'] == old['memberships'][acted_control_id])
        descriptor = prior['semantic_descriptor']
        previous_matches = [r for r in old['regions'] if r['semantic_descriptor'] == descriptor]
        current_matches = [r for r in new['regions'] if r['semantic_descriptor'] == descriptor]
        if len(previous_matches) != 1 or len(current_matches) != 1:
            raise ValueError('Previously used region is absent or structurally ambiguous; use fresh overview discovery')
        current = current_matches[0]
        result['fresh_region'] = progressive_ui.listing(after, region_id=current['id'],
            actions=actions, max_bytes=6000)
        result['region_continuity'] = {
            'basis': 'unique captured structural descriptor in both observations',
            'previous_region_id': prior['id'], 'current_region_id': current['id'],
            'region_members_may_have_changed': True, 'action_identity_proven': False,
            'role_query_or_goal_filter_applied': False,
            'next': 'Use these captured controls, page this list, inspect details or explore another region. Input still requires fresh guards.'}
    except (ValueError, KeyError, StopIteration) as error:
        result['region_continuity'] = {'status': 'unresolved', 'reason': str(error),
                                      'next': 'Inspect the new overview or another explicit region.'}
    return result
