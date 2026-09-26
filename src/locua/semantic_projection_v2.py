"""Opt-in compact exploration over the unchanged semantic-v1 guarded tools."""
from copy import deepcopy

from .model_interface import ModelInterface
from .semantic_projection import SemanticModelInterface


class SemanticV2ModelInterface(SemanticModelInterface):
    """Same observations, schemas and guards; compact stale exploration notes."""

    version = 'semantic-v2'

    def _project(self, name, raw, args=None):
        result = ModelInterface._project(self, name, raw, args)
        result['projection_version'] = self.version
        scope_id = raw.get('scope_id')
        translated_scope = (args or {}).get('scope_id')
        if scope_id is None:
            scope_id = translated_scope
        # A conflicting receipt must not suppress potentially relevant evidence.
        scope = (self.owner._scopes.get(scope_id)
                 if translated_scope in (None, scope_id) else None)
        if (scope and scope.get('status') in ('approved', 'reconciled_verified') and scope.get('goals')
                and all(g.get('kind') in ('text', 'state') for g in scope['goals'])):
            result.pop('arithmetic_input', None)
        return result

    def state(self, review=None, cursor=None):
        result = super().state(review, cursor)
        if review is not None or cursor is not None:
            return result
        current = {v['view'] for v in result['current_views']}
        explored = result['explored']
        for key, default_seen_as in (('controls', 'listed_control'), ('regions', 'overview')):
            compact = []
            for row in explored[key]:
                note = deepcopy(row)
                # These warnings apply to every entry, including current views.
                note.pop('potentially_stale', None)
                if note.get('seen_as') == default_seen_as:
                    note.pop('seen_as')
                if note.get('view') not in current:
                    # Keep visited identities without repeating superseded state.
                    for field in ('value', 'states', 'position'):
                        note.pop(field, None)
                compact.append(note)
            explored[key] = compact
        explored.update(format='compact_breadcrumbs_v2',
            older_view_fields_omitted=['value', 'states', 'position'],
            next='All entries may be stale. Old-view identities are breadcrumbs only; discover current controls through current_views.')
        return result
