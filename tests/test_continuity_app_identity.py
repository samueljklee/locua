"""CPU-only identity projection tests; no driver, GUI, or model calls."""
from copy import deepcopy
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from locua.amplifier_tools import DesktopToolset, _hash, _proven_installed_app
from locua.desktop_tools import _bundle_executable
from tests.test_amplifier_tools import Desktop


class InventoryDesktop(Desktop):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.started = 'Mon Sep 21 09:00:00 2026'
        self.targets = [(41, 52, 'Draft'), (77, 81, '')]

    def apps(self):
        self.calls.append(('apps',))
        return {'status': 'ok', 'apps': deepcopy(self.rows)}

    def app_windows(self, app):
        self.calls.append(('app_windows', deepcopy(app)))
        installed = _bundle_executable(app)
        return {'status': 'ok', 'windows': [
            {'pid': pid, 'window_id': wid, 'title': title, 'is_on_screen': True,
             'app_identity_evidence': {'installed_bundle': deepcopy(installed),
                'process': {'pid': pid, 'executable_path': installed['executable_path'],
                            'started_at_utc': self.started},
                'process_rechecked': True}}
            for pid, wid, title in self.targets]}


class LogicalAppIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.owners = []
        self.app = self.bundle('Editor', 'test.editor')

    def tearDown(self):
        for owner in self.owners:
            owner.close()
        self.tmp.cleanup()

    def bundle(self, name, bundle_id):
        root = self.root/(name+'.app')
        (root/'Contents'/'MacOS').mkdir(parents=True)
        (root/'Contents'/'Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': bundle_id, 'CFBundleExecutable': name}))
        (root/'Contents'/'MacOS'/name).write_text('Synthetic executable fixture; never run.')
        return {'name': name, 'bundle_id': bundle_id, 'launch_path': str(root),
                'pid': 41, 'running': True}

    def owner(self, rows, profile='continuity-v1'):
        desktop = InventoryDesktop(deepcopy(rows))
        owner = DesktopToolset({}, self.root/('run'+str(len(self.owners))),
            'Use the requested application.', lambda *_: 'run',
            desktop=desktop, tool_profile=profile)
        self.owners.append(owner)
        return owner, desktop

    def test_three_process_aliases_one_app_and_all_windows(self):
        rows = [dict(self.app, pid=pid) for pid in (41, 77, 88)]
        owner, desktop = self.owner(rows)
        result = owner.model_interface.call('locua_apps', {'query': 'Editor'})
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['total'], 1)
        app = result['items'][0]
        self.assertTrue(app['identity_proven'])
        self.assertEqual([p['pid'] for p in app['processes']], [41, 77, 88])
        windows = owner.model_interface.call('locua_windows', {'app_id': app['app_id']})
        self.assertEqual([w['title'] for w in windows['windows']], ['Draft', ''])
        self.assertEqual(len({w['window_id'] for w in windows['windows']}), 2)
        self.assertEqual(len([c for c in desktop.calls if c[0] == 'app_windows']), 1)
        self.assertEqual(len(owner._app_records), 3)  # Raw audit evidence retained.

    def test_unknown_and_competing_installed_identities_never_merge(self):
        other = self.bundle('Other installation', self.app['bundle_id'])
        different = self.bundle('Different bundle', 'test.other')
        unknown = dict(self.app, launch_path=str(self.root/'Absent.app'))
        conflicting = dict(self.app, bundle_id='test.wrong')
        rows = [self.app, dict(self.app, pid=77),
                dict(other, name=self.app['name']), dict(different, name=self.app['name']),
                unknown, deepcopy(unknown), conflicting]
        owner, _ = self.owner(rows)
        result = owner.model_interface.call('locua_apps', {})
        self.assertEqual(result['total'], 6, result)
        self.assertEqual(len({r['app_id'] for r in result['items']}), 6)
        self.assertEqual([r['identity_proven'] for r in result['items']],
                         [True, True, True, False, False, False])
        self.assertEqual(sum(len(r['processes']) for r in result['items']), len(rows))

    def test_grouping_precedes_pagination_and_keeps_retained_boundaries(self):
        second = self.bundle('Second', 'test.second')
        third = self.bundle('Third', 'test.third')
        rows = [self.app, second, dict(self.app, pid=77), third, dict(second, pid=88)]
        owner, desktop = self.owner(rows)
        ui = owner.model_interface
        first = ui.call('locua_apps', {'limit': 1})
        self.assertEqual(first['total'], 3)
        self.assertEqual(first['next_start'], 1)
        self.assertEqual([p['pid'] for p in first['items'][0]['processes']], [41, 77])
        # Installed identity cannot change page boundaries mid-inventory.
        Path(second['launch_path'], 'Contents', 'Info.plist').unlink()
        page2 = ui.call('locua_apps', {'inventory_id': first['inventory_id'], 'start': 1, 'limit': 1})
        page3 = ui.call('locua_apps', {'inventory_id': first['inventory_id'], 'start': 2, 'limit': 1})
        self.assertEqual(page2['items'][0]['name'], 'Second')
        self.assertEqual([p['pid'] for p in page2['items'][0]['processes']], [41, 88])
        self.assertEqual(page3['items'][0]['name'], 'Third')
        self.assertIsNone(page3['next_start'])
        self.assertEqual(len([c for c in desktop.calls if c[0] == 'apps']), 1)
        fresh = ui.call('locua_apps', {'limit': 32})
        self.assertEqual(fresh['total'], 4)  # Now each unproven Second row is separate.

    def test_malformed_bundle_stays_discoverable_but_never_coalesces(self):
        malformed = self.bundle('Malformed', 'test.malformed')
        Path(malformed['launch_path'], 'Contents', 'Info.plist').write_bytes(b'<plist><')
        owner, _ = self.owner([self.app, malformed, dict(malformed, pid=77)])
        result = owner.model_interface.call('locua_apps', {})
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['total'], 3)
        self.assertEqual([r['identity_proven'] for r in result['items']], [True, False, False])

    def test_distinct_declared_paths_are_not_collapsed_by_symlink_resolution(self):
        alias = self.root/'Alias.app'
        alias.symlink_to(self.app['launch_path'], target_is_directory=True)
        owner, _ = self.owner([self.app, dict(self.app, launch_path=str(alias), pid=77)])
        result = owner.model_interface.call('locua_apps', {})
        self.assertEqual(result['total'], 2)
        self.assertTrue(all(r['identity_proven'] for r in result['items']))
        self.assertNotEqual(result['items'][0]['app_id'], result['items'][1]['app_id'])

    def test_query_matching_alias_preserves_the_whole_installed_group(self):
        owner, _ = self.owner([self.app, dict(self.app, name='Editor alias', pid=77)])
        result = owner.model_interface.call('locua_apps', {'query': 'alias'})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['observed_names'], ['Editor', 'Editor alias'])
        self.assertEqual([p['pid'] for p in result['items'][0]['processes']], [41, 77])

    def test_public_app_and_window_refs_survive_pid_alias_refresh(self):
        owner, desktop = self.owner([self.app])
        ui = owner.model_interface
        app1 = ui.call('locua_apps', {})['items'][0]['app_id']
        windows1 = ui.call('locua_windows', {'app_id': app1})['windows']
        desktop.rows = [dict(self.app, pid=77)]
        app2 = ui.call('locua_apps', {})['items'][0]['app_id']
        windows2 = ui.call('locua_windows', {'app_id': app2})['windows']
        self.assertEqual(app1, app2)
        self.assertEqual(windows1, windows2)
        self.assertEqual(desktop.calls[-1][1]['pid'], 77)  # Routes to refreshed raw evidence.
        desktop.started = 'Mon Sep 21 09:10:00 2026'
        windows3 = ui.call('locua_windows', {'app_id': app2})['windows']
        self.assertNotEqual(windows2[0]['window_id'], windows3[0]['window_id'])
        executable = Path(self.app['launch_path'], 'Contents', 'MacOS', 'Editor')
        executable.write_text('Changed installation fingerprint')
        app3 = ui.call('locua_apps', {})['items'][0]['app_id']
        self.assertNotEqual(app2, app3)

    def test_raw_alias_window_refs_stable_only_with_complete_matching_proof(self):
        rows = [self.app, dict(self.app, pid=77)]
        owner, desktop = self.owner(rows)
        raw = owner.call('locua_apps', {})['items']
        one = owner.call('locua_windows', {'app_id': raw[0]['app_id']})
        two = owner.call('locua_windows', {'app_id': raw[1]['app_id']})
        self.assertEqual(one['windows'], two['windows'])
        result = desktop.app_windows(self.app)
        del result['windows'][0]['app_identity_evidence']['process']['started_at_utc']
        one = owner._windows_result(raw[0]['app_id'], result)
        two = owner._windows_result(raw[1]['app_id'], result)
        self.assertNotEqual(one['windows'][0]['window_id'], two['windows'][0]['window_id'])
        result = desktop.app_windows(self.app)
        result['windows'][0]['app_identity_evidence']['process']['pid'] = 999
        one = owner._windows_result(raw[0]['app_id'], result)
        two = owner._windows_result(raw[1]['app_id'], result)
        self.assertNotEqual(one['windows'][0]['window_id'], two['windows'][0]['window_id'])

    def test_legacy_profile_keeps_raw_apps_and_alias_specific_window_refs(self):
        owner, _ = self.owner([self.app, dict(self.app, pid=77)], profile='baseline')
        raw = owner.call('locua_apps', {})
        self.assertIsNone(owner.model_interface)
        self.assertEqual(raw['total'], 2)
        one = owner.call('locua_windows', {'app_id': raw['items'][0]['app_id']})
        two = owner.call('locua_windows', {'app_id': raw['items'][1]['app_id']})
        self.assertNotEqual(one['windows'][0]['window_id'], two['windows'][0]['window_id'])

    def test_locale_alias_resolution_requires_identical_proven_installation(self):
        owner, _ = self.owner([self.app])
        target = {'pid': 41, 'window_id': 52}
        def records(rows):
            owner._app_records = {str(n): r for n, r in enumerate(rows)}
            owner._window_records = {str(n): {'app_id': str(n), 'target': target}
                                     for n in range(len(rows))}
        records([self.app, dict(self.app, pid=77)])
        self.assertEqual(_proven_installed_app(owner._number_format_app(target)),
                         _proven_installed_app(self.app))
        owner.tool_profile = 'baseline'
        self.assertIsNone(owner._number_format_app(target))
        owner.tool_profile = 'continuity-v1'
        other = self.bundle('Duplicate installation', self.app['bundle_id'])
        records([self.app, other])
        self.assertIsNone(owner._number_format_app(target))
        records([self.app, dict(self.app, bundle_id='unproven')])
        self.assertIsNone(owner._number_format_app(target))
        records([self.app, None])
        self.assertIsNone(owner._number_format_app(target))
        records([])
        self.assertIsNone(owner._number_format_app(target))
        records([self.app])
        self.assertEqual(owner._number_format_app(target), self.app)

    def test_locale_probe_still_receives_the_exact_native_target(self):
        owner, desktop = self.owner([self.app, dict(self.app, pid=77)])
        # Reuse the existing guard fixture to exercise _verify_scopes rather
        # than asserting only the alias helper's return value.
        ui = owner.model_interface
        apps = ui.call('locua_apps', {})
        window = ui.call('locua_windows', {'app_id': apps['items'][0]['app_id']})['windows'][0]['window_id']
        view = ui.call('locua_observe', {'window_id': window})['view']
        readout = next(r for r in ui.call('locua_inspect', {'view': view})['items'] if r.get('name') == 'Result')
        review = ui.call('locua_review', {'summary': 'Calculate 2+3',
            'goals': [{'kind': 'calculation', 'target': readout['target'], 'expression': '2+3'}],
            'covers_request': True})
        self.assertEqual(review['status'], 'approved', review)
        target = {'pid': 41, 'window_id': 52}
        # Legacy aliases may remain in history; add those issued records too.
        for index, row in enumerate(desktop.rows):
            key = 'app:'+_hash(row)[:24]
            owner._window_records['older-alias-'+str(index)] = {'target': target, 'app_id': key, 'raw': {}}
        with patch.object(desktop, 'number_format', create=True,
                          return_value={'status': 'unknown', 'reason': 'fixture probe'}) as probe:
            ui.call('locua_verify', {})
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[1], target)
        self.assertEqual(_proven_installed_app(probe.call_args.args[0]), _proven_installed_app(self.app))


if __name__ == '__main__':
    unittest.main()
