#!/usr/bin/env python3
"""Independent saved-output oracle. Never imported by fixture server or engine."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

HERE=Path(__file__).resolve().parent
CASES=json.loads((HERE/'cases.json').read_text())['cases']

def initial_values(case):
    values=dict(case['structure']['initials'])
    if case['id']=='dispatch-board':values.update({f'reference.{i:02}':f'EV-{i:03}' for i in range(1,21)})
    return values

def verify_browser(case_id,receipts):
    case=next(c for c in CASES if c['id']==case_id and c['family']=='browser');path=Path(receipts)
    initial=initial_values(case);expected={**initial,**case['changes']};issues=[]
    rp=path/(case_id+'.json');ep=path/(case_id+'.events.jsonl')
    receipt=json.loads(rp.read_text()) if rp.exists() else None
    events=[json.loads(line) for line in ep.read_text().splitlines() if line] if ep.exists() else []
    if receipt is None:issues.append('no_persisted_receipt')
    if not events:issues.append('no_input_or_save_events')
    protected=set(initial)-set(case['changes']);preservation_checks=0
    for n,event in enumerate(events,1):
        if event.get('sequence')!=n or event.get('case_id')!=case_id:issues.append('event_identity_or_order')
        values=event.get('values',{})
        if set(values)!=set(initial) or any(type(values[k]) is not type(initial[k]) for k in set(values)&set(initial)):issues.append('event_field_shape')
        for key in protected:
            preservation_checks+=1
            if values.get(key)!=initial[key]:issues.append('preservation:'+key)
    if receipt:
        if receipt not in events:issues.append('receipt_not_in_event_journal')
        if receipt.get('values')!=expected:issues.append('final_exact_fields')
        if events and events[-1].get('values')!=expected:issues.append('post_receipt_state_changed')
        if case_id=='access-dialog' and receipt.get('kind')!='save':issues.append('explicit_save_absent')
    return {'schema':'locua.transfer-v8.browser-oracle.v1','case_id':case_id,'pass':not issues,
            'issues':sorted(set(issues)),'field_count':len(expected),'event_count':len(events),
            'protected_field_count':len(protected),'preservation_checks':preservation_checks,
            'explicit_save_events':sum(e.get('kind')=='save' for e in events),
            'receipt_sha256':hashlib.sha256(rp.read_bytes()).hexdigest() if rp.exists() else None,
            'limits':'Checks recorded page-input persistence; caller must separately audit actual model/guard/action provenance, reviewed scope and cleanup.'}

def read_cells(path):
    ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as z:
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            strings=[''.join(x.itertext()) for x in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('x:si',ns)]
        wb=ET.fromstring(z.read('xl/workbook.xml'))
        sheets=wb.findall('x:sheets/x:sheet',ns)
        if len(sheets)!=1 or sheets[0].get('name')!='Samples':raise ValueError('unexpected_sheet_inventory')
        rels={r.get('Id'):r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        rid=sheets[0].get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id');target=rels[rid]
        target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
        cells={}
        for c in ET.fromstring(z.read(target)).findall('.//x:sheetData/x:row/x:c',ns):
            f=c.find('x:f',ns);v=c.find('x:v',ns);t=c.get('t');inline=c.find('x:is',ns)
            if f is not None:val=('formula',f.text or '')
            elif t=='s':val=('string',strings[int(v.text)])
            elif t=='inlineStr':val=('string',''.join(inline.itertext()) if inline is not None else '')
            elif v is None or v.text is None:continue
            elif t=='b':val=('boolean',v.text=='1')
            elif t in ('str','e'):val=(t,v.text)
            else:val=('number',str(Decimal(v.text).normalize()))
            cells[c.get('r')]=val
        return cells

def verify_workbook(path):
    initial=read_cells(HERE/'sample-register.xlsx');actual=read_cells(path);expected={**initial,'F7':('string','Ready — lot 09')}
    issues=[k for k in sorted(set(expected)|set(actual)) if expected.get(k)!=actual.get(k)]
    return {'schema':'locua.transfer-v8.workbook-oracle.v1','pass':not issues,'mismatched_cells':issues,
            'checked_cells':len(expected),'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'evidence_plane':'saved_file','explicit_save_proven':False,
            'limits':'Saved typed cell/formula serialization only. Separate evidence is required for cell UI binding, committed state, explicit Save, workbook formatting and no unexpected actions.'}

def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='command',required=True)
    b=s.add_parser('browser');b.add_argument('--case',choices=['dispatch-board','access-dialog'],required=True);b.add_argument('--receipts',type=Path,required=True)
    w=s.add_parser('workbook');w.add_argument('path',type=Path)
    a=p.parse_args();result=verify_browser(a.case,a.receipts) if a.command=='browser' else verify_workbook(a.path)
    print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result['pass'] else 1)

if __name__=='__main__':main()
