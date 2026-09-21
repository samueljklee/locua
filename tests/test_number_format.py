from copy import deepcopy
from fractions import Fraction
import inspect
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from locua.number_format import evidence_from_probe, parse_display_number, probe_app_number_format, PROBE_SCHEMA

TARGET={'pid':17,'window_id':29}
BUNDLE='org.example.application'
NOW=2_000_000_000


def probe(decimal='.',group=',',primary=3,secondary=0,identifier='en_US'):
    format_={'locale_identifier':identifier,'decimal_separator':decimal,'grouping_separator':group,
        'primary_grouping_size':primary,'secondary_grouping_size':secondary,'uses_grouping_separator':True,
        'minus_sign':'-','plus_sign':'+','positive_prefix':'','negative_prefix':'-','digit_samples':list('0123456789')}
    keys=('AppleLocale','AppleLanguages','AppleICUNumberSymbols','AppleICUNumberFormatStrings')
    app={k:{'any_host':None,'current_host':None} for k in keys};global_=deepcopy(app)
    global_['AppleLocale']['any_host']=identifier
    return {'schema':PROBE_SCHEMA,'bundle_id':BUNDLE,'capture_started_at_ns':NOW-100_000_000,
        'capture_finished_at_ns':NOW-10_000_000,'current_foundation':deepcopy(format_),
        'effective_identifier_foundation':deepcopy(format_),'application_preferences':app,
        'global_preferences':global_,'effective_preferences':dict.fromkeys(keys)|{'AppleLocale':identifier}}


def evidence(**kwargs):return evidence_from_probe(probe(**kwargs),bundle_id=BUNDLE,target=TARGET,received_at_ns=NOW)


def parse(text,e=None,**kwargs):return parse_display_number(text,e or evidence(),expected_target=TARGET,expected_bundle_id=BUNDLE,now_ns=NOW,**kwargs)


class NumberFormatTests(unittest.TestCase):
    def assertNumber(self,text,value,e=None):
        got=parse(text,e)
        self.assertEqual(got['status'],'parsed_under_observed_locale',got)
        self.assertEqual(Fraction(got['numerator'],got['denominator']),Fraction(value));self.assertEqual(got['raw'],text)
        self.assertFalse(got['application_formatter_proven']);return got

    def test_display_grouping_from_independent_locale_not_expected_answer(self):
        got=self.assertNumber('\u200e44,252',44252)
        self.assertTrue(got['normalization']['grouping_validated'])
        self.assertEqual(got['normalization']['direction_marks_removed'],1)
        self.assertNotIn('expected_value',inspect.signature(parse_display_number).parameters)
        self.assertNotIn('expected_answer',inspect.signature(parse_display_number).parameters)

    def test_same_string_has_one_interpretation_per_independently_supplied_locale(self):
        self.assertNumber('1.234',Fraction(1234,1000),evidence())
        self.assertNumber('1.234',1234,evidence(decimal=',',group='.',identifier='de_DE'))
        self.assertNumber('1,234',1234,evidence())
        self.assertNumber('1,234',Fraction(1234,1000),evidence(decimal=',',group='.',identifier='de_DE'))

    def test_comma_decimal_and_exact_grouping_spaces(self):
        french=evidence(decimal=',',group='\u202f',identifier='fr_FR')
        self.assertNumber('1\u202f234,50',Fraction(2469,2),french)
        self.assertNumber('1234,50',Fraction(2469,2),french)
        self.assertEqual(parse('1 234,50',french)['status'],'unknown')
        self.assertEqual(parse('1\u00a0234,50',french)['status'],'unknown')

    def test_primary_and_secondary_grouping_strictly_validated(self):
        indian=evidence(primary=3,secondary=2,identifier='en_IN')
        self.assertNumber('12,34,567.89',Fraction(123456789,100),indian)
        self.assertNumber('1,234',1234,indian)
        for text in ('123,456,789','1,234,567','1,23','1,23,45'):
            self.assertEqual(parse(text,indian)['status'],'unknown',text)
        for text in ('44,25','4,4,252','442,52',',123','123,','1,,234','1234,567','1,23,456'):
            self.assertEqual(parse(text)['status'],'unknown',text)

    def test_whitespace_group_separator_not_discarded_as_outer_padding(self):
        spaced=evidence(decimal=',',group=' ',identifier='test_space')
        self.assertNumber('1 234,50',Fraction(2469,2),spaced)
        for value in (' 1 234','1 234 ','1 ',' 1'):
            self.assertEqual(parse(value,spaced)['status'],'unknown',value)
        self.assertEqual(evidence(decimal=' ',group=',')['status'],'unknown')

    def test_declared_app_locale_must_reconcile_with_effective_preference(self):
        p=probe();p['application_preferences']['AppleLocale']['any_host']='de_DE'
        self.assertEqual(evidence_from_probe(p,bundle_id=BUNDLE,target=TARGET,received_at_ns=NOW)['reason'],'application_locale_override_not_resolved')
        p['effective_preferences']['AppleLocale']=None
        self.assertEqual(evidence_from_probe(p,bundle_id=BUNDLE,target=TARGET,received_at_ns=NOW)['reason'],'application_locale_override_not_resolved')

    def test_sign_scientific_and_direction_marks_are_recorded_not_prose_stripped(self):
        self.assertNumber('\u200f−1,234.5e+2',-123450)
        self.assertNumber(' +.25 ',Fraction(1,4))
        for text in ('$44,252','44,252%','answer: 44,252','(44,252)','4.4,252','NaN','Infinity','1e999','1e2e3','１２３','1\u202e234'):
            self.assertEqual(parse(text)['status'],'unknown',text)

    def test_no_locale_or_contradictory_target_never_tries_an_alternate_format(self):
        self.assertEqual(parse_display_number('44,252',None,expected_target=TARGET,now_ns=NOW)['status'],'unknown')
        self.assertEqual(parse_display_number('44,252',evidence(),expected_target={'pid':18,'window_id':29},now_ns=NOW)['reason'],'locale_evidence_target_mismatch')
        self.assertEqual(parse_display_number('44,252',evidence(),expected_target=TARGET,expected_bundle_id='org.other',now_ns=NOW)['reason'],'locale_evidence_target_mismatch')

    def test_locale_freshness_and_integrity_required(self):
        e=evidence()
        self.assertEqual(parse_display_number('1',e,expected_target=TARGET,now_ns=NOW+61_000_000_000)['reason'],'locale_evidence_stale_or_future')
        self.assertEqual(parse_display_number('1',e,expected_target=TARGET,now_ns=1)['reason'],'locale_evidence_stale_or_future')
        e['format']['decimal_separator']=','
        self.assertEqual(parse('44,252',e)['reason'],'locale_evidence_integrity_mismatch')

    def test_missing_times_wrong_bundle_and_partial_preference_reads_stay_unknown(self):
        for mutate in (lambda p:p.pop('capture_started_at_ns'),lambda p:p.update(bundle_id='org.other'),
                       lambda p:p['application_preferences'].pop('AppleLocale'),
                       lambda p:p.update(capture_finished_at_ns=NOW+1)):
            p=probe();mutate(p)
            self.assertEqual(evidence_from_probe(p,bundle_id=BUNDLE,target=TARGET,received_at_ns=NOW)['status'],'unknown')

    def test_unresolved_custom_app_or_global_symbols_and_formats_refuse(self):
        for domain in ('application_preferences','global_preferences'):
            for key in ('AppleICUNumberSymbols','AppleICUNumberFormatStrings'):
                p=probe();p[domain][key]['any_host']={'custom':'x'}
                got=evidence_from_probe(p,bundle_id=BUNDLE,target=TARGET,received_at_ns=NOW)
                self.assertEqual(got['reason'],'custom_numeric_preferences_require_explicit_format_evidence')
        p=probe();p['application_preferences']['AppleLanguages']['any_host']=['de']
        self.assertEqual(evidence_from_probe(p,bundle_id=BUNDLE,target=TARGET,received_at_ns=NOW)['reason'],'application_language_override_without_explicit_locale')

    def test_explicit_app_locale_resolves_independently_but_does_not_certify_formatter(self):
        p=probe(decimal=',',group='.',identifier='de_DE')
        p['current_foundation']=probe()['current_foundation'];p['application_preferences']['AppleLocale']['any_host']='de_DE'
        e=evidence_from_probe(p,bundle_id=BUNDLE,target=TARGET,received_at_ns=NOW)
        self.assertEqual(e['status'],'locale_conventions_observed');self.assertFalse(e['application_formatter_proven'])
        self.assertNumber('44.252',44252,e)

    def test_ambiguous_separators_non_ascii_digits_or_unknown_group_sizes_refuse(self):
        for kwargs in ({'decimal':',','group':','},{'primary':0},{'secondary':-1}):
            self.assertEqual(evidence(**kwargs)['status'],'unknown')
        p=probe();p['effective_identifier_foundation']['digit_samples'][0]='٠'
        self.assertEqual(evidence_from_probe(p,bundle_id=BUNDLE,target=TARGET,received_at_ns=NOW)['status'],'unknown')

    def test_probe_uses_only_fixed_read_only_swift_and_exact_bundle_argument(self):
        calls=[]
        def runner(argv,**kwargs):
            calls.append((argv,kwargs))
            text=Path(argv[-2]).read_text()
            self.assertIn('CFPreferencesCopyValue',text);self.assertNotIn('CFPreferencesSet',text)
            self.assertNotIn('NSWorkspace',text);self.assertEqual(argv[-1],BUNDLE)
            self.assertEqual(kwargs['check'],False);self.assertEqual(kwargs['timeout'],30)
            return SimpleNamespace(returncode=0,stdout=json.dumps(probe()),stderr='')
        with patch('locua.number_format.sys.platform','darwin'),patch('locua.number_format.time.time_ns',return_value=NOW):
            got=probe_app_number_format(BUNDLE,TARGET,runner=runner)
        self.assertEqual(got['status'],'locale_conventions_observed');self.assertEqual(len(calls),1)
        self.assertEqual(probe_app_number_format('../private',TARGET,runner=runner)['status'],'unknown');self.assertEqual(len(calls),1)

    def test_probe_failure_and_non_macos_are_unknown_no_fallback(self):
        with patch('locua.number_format.sys.platform','linux'):
            self.assertEqual(probe_app_number_format(BUNDLE,TARGET)['reason'],'foundation_probe_requires_macos')
        with patch('locua.number_format.sys.platform','darwin'):
            got=probe_app_number_format(BUNDLE,TARGET,runner=lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='private detail'))
            self.assertEqual(got['reason'],'foundation_probe_failed');self.assertNotIn('private detail',json.dumps(got))
