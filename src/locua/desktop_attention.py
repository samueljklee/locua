"""Visible inspection marker; no application input or model attention claims."""
from copy import deepcopy
import math
import uuid


def rectangle(value):
    if not isinstance(value, dict):
        return None
    keys = ('x', 'y', 'width', 'height')
    if not all(type(value.get(k)) in (int, float) and math.isfinite(value[k]) for k in keys):
        return None
    if value['width'] <= 0 or value['height'] <= 0:
        return None
    return {k: value[k] for k in keys}


def inside(inner, outer):
    return (inner['x'] >= outer['x'] and inner['y'] >= outer['y']
            and inner['x'] + inner['width'] <= outer['x'] + outer['width'] + 1
            and inner['y'] + inner['height'] <= outer['y'] + outer['height'] + 1)


class VisibleDesktopOwner:
    """One transport/desktop lease, one visible native cursor for reads and input.

    Driver-global inventory calls retain the implicit lifecycle. Native scoped
    calls share a single named lifecycle and cursor; both close with the owner.
    No parallel desktop controller and no modification to the legacy Cua owner.
    """
    def __init__(self, owner):
        self.owner = owner
        self.schemas = {t['name']: t['inputSchema'] for t in owner.inventory.get('tools', [])}
        self.label = 'Locua-' + uuid.uuid4().hex[:8]
        self.ready = False
        self.error = None

    def __getattr__(self, name):
        return getattr(self.owner, name)

    def ensure_cursor(self):
        if self.ready:
            return
        required = {'move_cursor', 'set_agent_cursor_enabled', 'set_agent_cursor_motion'}
        if not required <= set(self.schemas):
            raise ValueError('This driver does not advertise the inspection cursor tools')
        if self.error:
            raise ValueError(self.error)
        try:
            self.owner.start_session(self.label)
            self.owner.call('set_agent_cursor_enabled', {'session': self.label, 'enabled': True})
            self.owner.call('set_agent_cursor_motion', {'session': self.label, 'idle_hide_ms': 0})
            self.ready = True
        except Exception as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)
            raise

    def call(self, name, args, **kwargs):
        # Unsupported cursor inventories preserve existing nonvisual operation.
        if (name not in ('start_session', 'end_session') and 'session' not in args
                and 'session' in self.schemas.get(name, {}).get('properties', {})
                and {'move_cursor', 'set_agent_cursor_motion', 'set_agent_cursor_enabled'} <= set(self.schemas)):
            try:
                self.ensure_cursor()
            except Exception:
                return self.owner.call(name, args, **kwargs)
            args = {**args, 'session': self.label}
        return self.owner.call(name, args, **kwargs)

    def move(self, area):
        self.ensure_cursor()
        point = {'x': area['x'] + area['width'] / 2, 'y': area['y'] + area['height'] / 2}
        self.owner.call('move_cursor', {'session': self.label, 'scope': 'window', **point})
        return {'position': point, 'session': self.label, 'scope': 'window',
                'application_input': False, 'real_pointer_moved': False,
                'pixels_independently_verified': False, 'idle_fading': False}


def area_for(observation, window, control_ids=None):
    """Union of returned, visibly bounded controls; never rank competing targets."""
    if control_ids is None:
        return deepcopy(window)
    identifiers = set(control_ids)
    areas = [rectangle(c.get('bounds')) for c in observation['controls'] if c['id'] in identifiers]
    areas = [r for r in areas if r and inside(r, window)]
    if not areas:
        return None
    x = min(r['x'] for r in areas);y = min(r['y'] for r in areas)
    return {'x': x, 'y': y,
            'width': max(r['x'] + r['width'] for r in areas) - x,
            'height': max(r['y'] + r['height'] for r in areas) - y}
