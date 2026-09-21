"""Synthetic peers only: no Excel, Apple events, permission queries or UI."""
from copy import deepcopy
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from locua.engine.excel import ExcelAdapter, ExcelError, range_addresses, verify_saved_addresses

TARGET = {'pid': 42, 'executable': '/Applications/Microsoft Excel.app/Contents/MacOS/Microsoft Excel',
          'workbook_name': 'Form.xlsx', 'workbook_full_name': '/tmp/Form.xlsx', 'sheet': 'Data'}
FALSE = {'type':'boolean','value':False}


def cell(address, value):
    return {'address':address, 'value2':deepcopy(value), 'formula':deepcopy(value),
            'has_formula':deepcopy(FALSE), 'value_stable':True, 'merged':deepcopy(FALSE),
            'array_formula_member':deepcopy(FALSE), 'plane':'committed_document',
            'editor_buffer_proven':False, 'saved_file_proven':False}


class Peer:
    def __init__(self):
        self.calls = []
        self.cells = {'A1':cell('A1',{'type':'string','value':'Name'}),
                      'B1':cell('B1',{'type':'blank','value':None}),
                      'C1':cell('C1',{'type':'string','value':'  before\n'})}
        self.reply_patch = None
    def __call__(self, request):
        self.calls.append(deepcopy(request))
        reference = request['range']
        if request['operation'] == 'set_cell':
            actual = self.cells[reference]
            if any(actual[k] != request['expected_before'][k] for k in ('value2','formula','has_formula')):
                return {'schema':'locua.excel.events.v1','status':'refused','action_started':False}
            value = request['desired']
            actual['value2'] = deepcopy(value)
            actual['formula'] = deepcopy(value)
        result = {'schema':'locua.excel.events.v1','status':'observed','operation':request['operation'],
                  **TARGET,'range':reference,'cells':[deepcopy(self.cells[a]) for a in range_addresses(reference)],
                  'workbook_read_only':deepcopy(FALSE),'sheet_protected':deepcopy(FALSE),
                  'action_started': request['operation'] != 'observe',
                  'capture_started_at_ns':time.time_ns(), 'capture_finished_at_ns':time.time_ns()}
        if self.reply_patch:
            self.reply_patch(result)
        return result


class ExcelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.peer = Peer()
        self.adapter = ExcelAdapter(TARGET, self.peer)

    def test_layout_independent_label_with_gutter(self):
        obs = self.adapter.observe('A1:C1')
        self.assertEqual(self.adapter.resolve_label(obs,'Name','right'),'B1')
        self.assertEqual(self.adapter.resolve_label(obs,'Name','unique_nonblank_right'),'C1')
        self.peer.cells['B1'] = cell('B1', {'type':'string','value':'competitor'})
        obs = self.adapter.observe('A1:C1')
        with self.assertRaises(ExcelError):
            self.adapter.resolve_label(obs,'Name','unique_nonblank_right')

    def test_unknown_competitor_and_empty_formula_count_as_binding_evidence(self):
        self.peer.cells['B1']['value_stable']=False
        with self.assertRaises(ExcelError):
            self.adapter.resolve_label(self.adapter.observe('A1:C1'),'Name')
        self.peer.cells['B1']['value_stable']=True
        self.peer.cells['B1']['has_formula']={'type':'boolean','value':True}
        self.peer.cells['B1']['value2']={'type':'string','value':''}
        with self.assertRaises(ExcelError):
            self.adapter.resolve_label(self.adapter.observe('A1:C1'),'Name','unique_nonblank_right')

    def test_competing_label_and_unobserved_adjacent_refused(self):
        self.peer.cells['C1'] = cell('C1', {'type':'string','value':'Name'})
        with self.assertRaises(ExcelError):
            self.adapter.resolve_label(self.adapter.observe('A1:C1'),'Name')
        with self.assertRaises(ExcelError):
            self.adapter.resolve_label(self.adapter.observe('A1'),'Name')

    def test_exact_text_and_empty_commit_never_imply_saved_or_editor(self):
        for text in ('  after\n',''):
            obs = self.adapter.observe('C1')
            result = self.adapter.set_cell(obs,'C1',{'type':'string','value':text},authorize_write=True)
            self.assertEqual(result['status'],'committed_verified')
            self.assertFalse(result['saved_file_proven'])
            self.assertFalse(result['editor_buffer_proven'])
            self.assertEqual(result['observation']['cells'][0]['value2']['value'],text)
            with self.assertRaises(ExcelError):
                self.adapter.set_cell(obs,'C1',{'type':'string','value':'again'},authorize_write=True)

    def test_stale_or_tampered_or_foreign_snapshot_has_zero_write_dispatch(self):
        obs = self.adapter.observe('C1')
        bad = deepcopy(obs); bad['cells'][0]['value2']['value'] = 'forged'
        with self.assertRaises(ExcelError):
            self.adapter.set_cell(bad,'C1',{'type':'string','value':'x'},authorize_write=True)
        other = ExcelAdapter(TARGET,self.peer)
        with self.assertRaises(ExcelError):
            other.set_cell(obs,'C1',{'type':'string','value':'x'},authorize_write=True)
        with patch('locua.engine.excel.time.time_ns',return_value=obs['observed_at_ns']+31_000_000_000):
            with self.assertRaises(ExcelError):
                self.adapter.set_cell(obs,'C1',{'type':'string','value':'x'},authorize_write=True)
        self.assertEqual(len(self.peer.calls),1)

    def test_missing_or_incoherent_source_times_never_gain_freshness(self):
        for mutate in (lambda r:r.pop('capture_started_at_ns'),
                       lambda r:r.update(capture_finished_at_ns=1),
                       lambda r:r.update(capture_started_at_ns=time.time_ns()+30_000_000_000),
                       lambda r:r.update(capture_started_at_ns=time.time_ns()-20_000_000_000)):
            self.peer.reply_patch=mutate
            with self.assertRaises(ExcelError):self.adapter.observe('C1')
        self.assertTrue(all(r['operation']=='observe' for r in self.peer.calls))

    def test_wrong_operation_cannot_acknowledge_save(self):
        obs=self.adapter.observe('C1')
        self.peer.reply_patch=lambda r:r.update(operation='observe')
        self.assertEqual(self.adapter.save(obs,authorize_save_workbook=True)['status'],'effect_unknown')

    def test_source_change_before_write_refuses_without_retry(self):
        obs = self.adapter.observe('C1')
        self.peer.cells['C1']['value2']['value'] = 'user edit'
        result = self.adapter.set_cell(obs,'C1',{'type':'string','value':'x'},authorize_write=True)
        self.assertEqual(result['status'],'refused')
        self.assertFalse(result['retry_allowed'])
        self.assertEqual(self.peer.cells['C1']['value2']['value'],'user edit')

    def test_wrong_identity_or_incomplete_observation_never_mints_snapshot(self):
        for mutate in (lambda r:r.update(pid=43), lambda r:r['cells'].pop(),
                       lambda r:r.update(range='B1'), lambda r:r['cells'].append(deepcopy(r['cells'][0]))):
            self.peer.reply_patch=mutate
            with self.assertRaises(ExcelError):
                self.adapter.observe('A1:C1')

    def test_unknown_writability_merge_or_array_refuses_write(self):
        for field in ('merged','array_formula_member'):
            self.peer.cells['C1'][field]={'type':'boolean','value':True}
            obs=self.adapter.observe('C1')
            with self.assertRaises(ExcelError):
                self.adapter.set_cell(obs,'C1',{'type':'string','value':'x'},authorize_write=True)
            self.peer.cells['C1'][field]=deepcopy(FALSE)
        self.peer.reply_patch=lambda r:r.pop('workbook_read_only')
        obs=self.adapter.observe('C1')
        with self.assertRaises(ExcelError):
            self.adapter.set_cell(obs,'C1',{'type':'string','value':'x'},authorize_write=True)
        self.assertTrue(all(r['operation']=='observe' for r in self.peer.calls))

    def test_post_dispatch_wrong_identity_is_unknown_effect(self):
        obs=self.adapter.observe('C1')
        self.peer.reply_patch=lambda r:r.update(pid=43)
        result=self.adapter.set_cell(obs,'C1',{'type':'string','value':'x'},authorize_write=True)
        self.assertEqual(result['status'],'effect_unknown')
        self.assertFalse(result['retry_allowed'])

    def test_save_acknowledgment_needs_scope_but_not_saved_proof(self):
        obs=self.adapter.observe('C1')
        with self.assertRaises(ExcelError):
            self.adapter.save(obs)
        result=self.adapter.save(obs,authorize_save_workbook=True)
        self.assertEqual(result['status'],'save_acknowledged')
        self.assertFalse(result['saved_file_proven'])

    def test_validation_preserves_literal_formula_and_numeric_types(self):
        obs=self.adapter.observe('C1')
        for desired in ({'type':'number','value':True},{'type':'number','value':2**54},
                        {'type':'number','value':float('nan')},{'type':'string','value':'=1+1'},
                        {'type':'blank','value':None}):
            with self.assertRaises(ExcelError):
                self.adapter.set_cell(obs,'C1',desired,authorize_write=True)
        for reference in ('A0','XFE1','A1:C99','[Other]A1','B2:A1','A1,B2'):
            with self.assertRaises(ExcelError):range_addresses(reference)
        self.assertEqual(len(self.peer.calls),1)

    def test_saved_file_verifier_distinguishes_formula_cache_and_exact_whitespace(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'synthetic.xlsx'
            with ZipFile(path,'w') as z:
                z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="s1"/></sheets></workbook>')
                z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="s1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
                z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="2"><c r="B2" t="inlineStr"><is><t xml:space="preserve">  exact\n</t></is></c><c r="C2"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>')
            result=verify_saved_addresses(path,'Data',{'B2':{'type':'string','value':'  exact\n'},'C2':{'type':'formula','value':'=1+1'}})
            self.assertEqual(result['status'],'pass')
            self.assertEqual(result['plane'],'saved_file')
            self.assertFalse(result['formula_results_proven'])
            self.assertEqual(verify_saved_addresses(path,'Data',{'C2':{'type':'number','value':2}})['status'],'fail')

if __name__=='__main__':unittest.main()
