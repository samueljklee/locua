"""Public action IDs preserve the entire immutable source catalog; no app IO."""
from copy import deepcopy
import hashlib
import json
import unittest

from locua.action_references import public_catalog, ActionReferenceError


def actions():
    target={'pid':41,'window_id':52}
    return [{'id':'private-hash-a','kind':'press','control_id':'native:s1:1','snapshot_id':'s1',
             'target':deepcopy(target),'name':'Repeated label','role':'AXButton',
             'description':'Press observed button','requires_value':False},
            {'id':'private-hash-b','kind':'press','control_id':'native:s1:2','snapshot_id':'s1',
             'target':deepcopy(target),'name':'Repeated label','role':'AXButton',
             'description':'Press competing button','requires_value':False},
            {'id':'private-hash-c','kind':'set_text','control_id':'native:s1:3','snapshot_id':'s1',
             'target':deepcopy(target),'name':'  quoted "Ω"\r\nfield  ','role':'AXTextArea',
             'description':'Replace editor text','requires_value':True,'metadata':{'nested':[False,0,None]}}]


class ActionReferencesTests(unittest.TestCase):
    def test_complete_ordered_catalog_preserved_and_exact_private_descriptors(self):
        source=actions();before=deepcopy(source)
        digest=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()
        public,private=public_catalog(source,7)
        self.assertEqual([a['id'] for a in public],['action:7:1','action:7:2','action:7:3'])
        self.assertEqual(list(private),[a['id'] for a in public])
        self.assertEqual(list(private.values()),source)
        for row,original in zip(public,source):
            self.assertEqual({k:v for k,v in row.items() if k!='id'},
                             {k:v for k,v in original.items() if k!='id'})
        self.assertEqual(source,before)
        self.assertEqual(hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest(),digest)

    def test_aliases_do_not_depend_on_labels_values_roles_or_task(self):
        first=actions();second=deepcopy(first)
        for i,row in enumerate(second):
            row.update(name='unrelated '+str(i),value='different',description='No task filtering',kind='new_observed_kind')
        a,_=public_catalog(first,2);b,_=public_catalog(second,2)
        self.assertEqual([r['id'] for r in a],[r['id'] for r in b])
        self.assertEqual(len(b),len(first))

    def test_capture_ordinal_namespaces_aliases_without_changing_driver_snapshot(self):
        a,ma=public_catalog(actions(),1);b,mb=public_catalog(actions(),2)
        self.assertFalse(set(ma)&set(mb));self.assertEqual(list(ma.values()),list(mb.values()))
        self.assertTrue(all(r['snapshot_id']=='s1' for r in a+b))

    def test_public_private_and_input_objects_are_independent(self):
        original=actions();public,private=public_catalog(original,1)
        public[0]['target']['pid']=999;public[2]['metadata']['nested'].append('changed')
        self.assertEqual(private['action:1:1']['target']['pid'],41)
        self.assertEqual(original[0]['target']['pid'],41)
        private['action:1:3']['metadata']['nested'].append('private change')
        self.assertEqual(original[2]['metadata']['nested'],[False,0,None])
        self.assertNotIn('private change',public[2]['metadata']['nested'])

    def test_duplicate_source_ids_refuse_even_when_descriptors_match(self):
        source=actions();source.append(deepcopy(source[0]))
        with self.assertRaisesRegex(ActionReferenceError,'Duplicate'):public_catalog(source,1)

    def test_mixed_snapshot_window_process_or_session_refused(self):
        changes=[lambda a:a.update(snapshot_id='s2'),lambda a:a['target'].update(pid=42),
                 lambda a:a['target'].update(window_id=53),lambda a:a['target'].update(session='different')]
        for mutate in changes:
            with self.subTest(mutate=mutate):
                source=actions();mutate(source[-1])
                with self.assertRaises(ActionReferenceError):public_catalog(source,1)

    def test_positive_integer_capture_ordinal_only(self):
        for value in (0,-1,False,True,1.0,'1',None,[],{}):
            with self.subTest(value=value),self.assertRaises(ActionReferenceError):public_catalog(actions(),value)

    def test_malformed_identity_and_target_rejected_before_return(self):
        for key in ('id','kind','control_id','snapshot_id'):
            for value in ('','  ',None,1,False,[],{},'bad\x00id'):
                with self.subTest(key=key,value=value):
                    source=actions();source[-1][key]=value
                    with self.assertRaises(ActionReferenceError):public_catalog(source,1)
        for target in ({},{'pid':41},{'pid':41,'window_id':0},{'pid':True,'window_id':52},
                       {'pid':41,'window_id':'52'},{'pid':41,'window_id':52,'session':''},
                       {'pid':41,'window_id':52,'unexpected':'field'},{'target_id':'browser','tab_id':1}):
            with self.subTest(target=target):
                source=actions();source[-1]['target']=target
                with self.assertRaises(ActionReferenceError):public_catalog(source,1)

    def test_non_json_flags_and_catalog_shapes_rejected(self):
        for bad in (None,{},(),[None],['row']):
            with self.subTest(bad=bad),self.assertRaises(ActionReferenceError):public_catalog(bad,1)
        for field,value in (('requires_value',1),('metadata',float('nan')),('metadata',object())):
            with self.subTest(field=field,value=value):
                source=actions();source[-1][field]=value
                with self.assertRaises(ActionReferenceError):public_catalog(source,1)

    def test_empty_catalog_and_valid_session(self):
        self.assertEqual(public_catalog([],1),([],{}))
        source=actions()
        for a in source:a['target']['session']='owned'
        public,private=public_catalog(source,1)
        self.assertTrue(all(a['target']['session']=='owned' for a in public))
        self.assertEqual(list(private.values()),source)


if __name__=='__main__':unittest.main()
