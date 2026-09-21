"""Observed arithmetic input capabilities and an issuance witness, not a recipe.

No application names, coordinates, desired answer or next-key sequence enters
classification. The selector chooses among all observed controls; this guard
admits only recognized arithmetic input semantics for a calculation goal.
"""
import re

_WORDS = {**dict(zip(('zero','one','two','three','four','five','six','seven','eight','nine'),'0123456789')),
    'add':'+','plus':'+','subtract':'-','minus':'-','multiply':'*','times':'*',
    'divide':'/','divided by':'/','decimal point':'.','decimal':'.',
    'equals':'=','equal':'=','equal to':'=','calculate':'=',
    'clear':'clear_entry','all clear':'clear','clear entry':'clear_entry',
    'left parenthesis':'(','right parenthesis':')',
    'open parenthesis':'(','close parenthesis':')'}
_SYMBOLS = {'×':'*','÷':'/','−':'-','AC':'clear','C':'clear_entry'}


def symbol(control):
    if control.get('role') not in ('AXButton','button'):return None
    # Window management/menu buttons are not arithmetic input capabilities.
    names=[control.get('name'),control.get('semantics',{}).get('description'),control.get('semantics',{}).get('title')]
    found=set()
    for name in names:
        if not isinstance(name,str):continue
        text=name.strip()
        if text in _SYMBOLS:found.add(_SYMBOLS[text])
        elif len(text)==1 and text in '0123456789.+-*/()=':found.add(text)
        elif text.casefold() in _WORDS:found.add(_WORDS[text.casefold()])
    return next(iter(found)) if len(found)==1 else None


def normalized(expression):
    return re.sub(r'\s+','',expression).replace('×','*').replace('÷','/').replace('−','-')


class InputWitness:
    def __init__(self):self.entered='';self.evaluated=None;self.events=[];self.known_start=False
    def record(self, token, *, snapshot_id, descriptor, issued=True):
        self.events.append({'input':token,'post_snapshot':snapshot_id,'control':descriptor,'issued':issued})
        if not issued:return
        if token=='clear':self.entered='';self.evaluated=None;self.known_start=True
        elif token=='clear_entry':
            # Generic Clear/C may affect only the current operand. The label
            # does not prove its extent or reset any pending operator/operand.
            # Retain the event but discard any full-input witness.
            self.entered='';self.evaluated=None;self.known_start=False
        elif token=='=':self.evaluated=self.entered if self.known_start else None
        elif isinstance(token,str) and len(token)==1 and token in '0123456789.+-*/()':
            self.entered+=token;self.evaluated=None
        else:raise ValueError('Unrecognized arithmetic input witness')
    def record_replacement(self,text,*,snapshot_id,descriptor):
        self.entered=normalized(text);self.evaluated=None;self.known_start=True
        self.events.append({'replacement':text,'post_snapshot':snapshot_id,'control':descriptor,'issued':True})
    def matches(self,expression):
        return self.known_start and self.evaluated==normalized(expression)
    def view(self):
        return {'issued_expression_since_clear':self.entered,'issued_evaluation':self.evaluated,
                'known_start':self.known_start,
                'verification_prerequisites':{
                    'known_start':'Issued observed All Clear/AC or whole-expression replacement; a fresh zero display, generic Clear/C or Clear Entry is insufficient.',
                    'evaluation':'Issue evaluation after entering the reviewed expression from a known start.',
                    'result':'Independent fresh bound UI readback must match the reviewed calculation.'},
                'missing_issuance_prerequisites':
                    ([] if self.known_start else ['known_start'])+
                    ([] if self.evaluated is not None else ['evaluation_from_known_start']),
                'note':'Input issuance witness only; final result requires independent fresh bound UI readback.'}
