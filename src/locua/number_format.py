"""Strict numeric interpretation under independently observed locale conventions.

No expected result, app-specific UI labels, model output, or implicit locale
fallback is used. Foundation/CFPreferences evidence describes configured locale
conventions; it does NOT instrument an application's private formatter. Callers
must preserve that distinction when reporting displayed numeric verification.

Primary APIs: developer.apple.com/documentation/foundation/numberformatter
and developer.apple.com/documentation/corefoundation/cfpreferencescopyappvalue(_:_:).
"""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

VERSION = 'locua.number-format.v1'
PROBE_SCHEMA = 'locua.foundation-number-format.v1'
_KEYS = ('AppleLocale','AppleLanguages','AppleICUNumberSymbols','AppleICUNumberFormatStrings')
_FORMAT_KEYS = ('locale_identifier','decimal_separator','grouping_separator','primary_grouping_size',
                'secondary_grouping_size','uses_grouping_separator','minus_sign','plus_sign',
                'positive_prefix','negative_prefix','digit_samples')
_SWIFT = r'''
import Foundation
import CoreFoundation
let started = UInt64(Date().timeIntervalSince1970 * 1_000_000_000)
let bundle = CommandLine.arguments.last!
let keys = ["AppleLocale", "AppleLanguages", "AppleICUNumberSymbols", "AppleICUNumberFormatStrings"]
func read(_ key: String, _ app: CFString, _ host: CFString) -> Any {
    return CFPreferencesCopyValue(key as CFString, app, kCFPreferencesCurrentUser, host) ?? NSNull()
}
func format(_ locale: Locale) -> [String: Any] {
    let f = NumberFormatter(); f.locale = locale; f.numberStyle = .decimal
    return ["locale_identifier": locale.identifier, "decimal_separator": f.decimalSeparator ?? NSNull() as Any,
        "grouping_separator": f.groupingSeparator ?? NSNull() as Any, "primary_grouping_size": f.groupingSize,
        "secondary_grouping_size": f.secondaryGroupingSize, "uses_grouping_separator": f.usesGroupingSeparator,
        "minus_sign": f.minusSign ?? NSNull() as Any, "plus_sign": f.plusSign ?? NSNull() as Any,
        "positive_prefix": f.positivePrefix ?? NSNull() as Any, "negative_prefix": f.negativePrefix ?? NSNull() as Any,
        "digit_samples": (0...9).map { f.string(from:NSNumber(value:$0)) ?? "" }]
}
var app: [String: Any] = [:], global: [String: Any] = [:], effective: [String: Any] = [:]
for key in keys {
    app[key] = ["any_host": read(key, bundle as CFString, kCFPreferencesAnyHost), "current_host": read(key, bundle as CFString, kCFPreferencesCurrentHost)]
    global[key] = ["any_host": read(key, kCFPreferencesAnyApplication, kCFPreferencesAnyHost), "current_host": read(key, kCFPreferencesAnyApplication, kCFPreferencesCurrentHost)]
    effective[key] = CFPreferencesCopyAppValue(key as CFString, bundle as CFString) ?? NSNull()
}
let explicit = effective["AppleLocale"] as? String
let result: [String:Any] = ["schema":"locua.foundation-number-format.v1", "bundle_id":bundle,
    "current_foundation":format(Locale.current), "effective_identifier_foundation":explicit.map { format(Locale(identifier:$0)) } ?? [:],
    "application_preferences":app, "global_preferences":global, "effective_preferences":effective,
    "capture_started_at_ns":started,"capture_finished_at_ns":UInt64(Date().timeIntervalSince1970 * 1_000_000_000)]
let data = try JSONSerialization.data(withJSONObject:result,options:[.sortedKeys])
print(String(data:data,encoding:.utf8)!)
'''
PROBE_SHA256 = hashlib.sha256(_SWIFT.encode()).hexdigest()


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _target(target):
    return (isinstance(target,dict) and set(target)=={'pid','window_id'}
            and all(type(v) is int and v>0 for v in target.values()))


def _bundle(bundle):
    return isinstance(bundle,str) and re.fullmatch(r'[A-Za-z0-9]+(?:[A-Za-z0-9.-]*[A-Za-z0-9])?',bundle) is not None


def _unknown(reason,**details):
    return {'schema':VERSION,'status':'unknown','reason':reason,'application_formatter_proven':False,**details}


def _validate_format(value):
    if not isinstance(value,dict) or not set(_FORMAT_KEYS).issubset(value):return 'formatter_properties_missing'
    if not isinstance(value['locale_identifier'],str) or not value['locale_identifier']:return 'locale_identifier_missing'
    dec,group=value['decimal_separator'],value['grouping_separator']
    if not isinstance(dec,str) or not isinstance(group,str) or len(dec)!=1 or len(group)>1 or dec==group or dec.isspace():
        return 'ambiguous_or_unsupported_separators'
    if any(char in '0123456789eE+-\u2212\u200e\u200f\r\n\t' for char in dec+group):return 'ambiguous_or_unsupported_separators'
    if type(value['uses_grouping_separator']) is not bool:return 'grouping_mode_unknown'
    for key in ('primary_grouping_size','secondary_grouping_size'):
        if type(value[key]) is not int or not 0<=value[key]<=9:return 'grouping_size_unknown'
    if group and value['primary_grouping_size']==0:return 'grouping_size_unknown'
    if value['minus_sign'] not in ('-','\u2212') or value['plus_sign']!='+':return 'unsupported_sign_conventions'
    if value['positive_prefix']!='' or value['negative_prefix']!=value['minus_sign']:return 'unsupported_prefix_conventions'
    if value['digit_samples']!=list('0123456789'):return 'non_ascii_numerals_not_supported'
    return None


def evidence_from_probe(payload,*,bundle_id,target,received_at_ns=None):
    """Normalize a trusted local probe, including explicit unknown states.

    Caller supplies an already established app↔PID/window binding. This helper
    never infers application identity from a title or the displayed number.
    """
    if not _bundle(bundle_id) or not _target(target):return _unknown('exact_application_target_required')
    now=time.time_ns() if received_at_ns is None else received_at_ns
    if not isinstance(payload,dict) or payload.get('schema')!=PROBE_SCHEMA or payload.get('bundle_id')!=bundle_id:
        return _unknown('foreign_or_missing_probe_identity')
    start,end=payload.get('capture_started_at_ns'),payload.get('capture_finished_at_ns')
    if (type(now) is not int or type(start) is not int or type(end) is not int or not 0<start<=end<=now
            or end-start>30_000_000_000):return _unknown('invalid_probe_capture_interval')
    app,global_,effective=(payload.get(k) for k in ('application_preferences','global_preferences','effective_preferences'))
    if any(not isinstance(d,dict) or not set(_KEYS).issubset(d) for d in (app,global_,effective)):
        return _unknown('preference_lookup_incomplete')
    for domain in (app,global_):
        for key in _KEYS:
            if not isinstance(domain[key],dict) or set(domain[key])!={'any_host','current_host'}:
                return _unknown('preference_lookup_incomplete')
    overrides={k:any(v is not None for v in app[k].values()) for k in _KEYS}
    for key in ('AppleICUNumberSymbols','AppleICUNumberFormatStrings'):
        if effective[key] is not None or any(v is not None for d in (app,global_) for v in d[key].values()):
            return _unknown('custom_numeric_preferences_require_explicit_format_evidence',preference_key=key)
    # Per-app language choice can affect process locale resolution. Without an
    # explicit app locale, do not assume our helper process matches that app.
    if overrides['AppleLanguages'] and not overrides['AppleLocale']:
        return _unknown('application_language_override_without_explicit_locale')
    identifier=effective['AppleLocale']
    if identifier is not None and (not isinstance(identifier,str) or not identifier):
        return _unknown('invalid_effective_locale_preference')
    if overrides['AppleLocale'] and identifier not in [v for v in app['AppleLocale'].values() if v is not None]:
        return _unknown('application_locale_override_not_resolved')
    selected=payload.get('effective_identifier_foundation') if identifier else payload.get('current_foundation')
    error=_validate_format(selected)
    if error:return _unknown(error)
    if not overrides['AppleLocale']:
        current=payload.get('current_foundation')
        if _validate_format(current) or any(current[k]!=selected[k] for k in _FORMAT_KEYS):
            return _unknown('current_and_effective_locale_conventions_disagree')
    result={'schema':VERSION,'status':'locale_conventions_observed','bundle_id':bundle_id,
        'target':deepcopy(target),'observed_at_ns':end,'capture_started_at_ns':start,
        'format':{k:deepcopy(selected[k]) for k in _FORMAT_KEYS},
        'basis':'Foundation.NumberFormatter decimal style + selected-app CFPreferencesCopyAppValue',
        'application_preference_overrides':overrides,'custom_numeric_preferences_present':False,
        'application_formatter_proven':False,'target_identity_supplied_by_caller':True,
        'probe_source_sha256':PROBE_SHA256,
        'limitations':['Configured locale conventions are observed independently of the displayed/expected number.',
            'An application may implement or cache a private formatter; this probe does not instrument it.',
            'No editor, document, saved-file or mathematical-operation completion is proved.']}
    result['evidence_sha256']=_hash(result);return result


def probe_app_number_format(bundle_id,target,*,timeout_s=30,cache_dir=None,runner=subprocess.run):
    """Read-only macOS probe; no GUI/Apple Events, preference writes or network.

    Reads only four named preference keys for the selected bundle/global locale.
    No helper/runtime is started on import. A compiler cache may be supplied in
    the private run directory; it is not a cache of application observations.
    """
    if not _bundle(bundle_id) or not _target(target):return _unknown('exact_application_target_required')
    if sys.platform!='darwin':return _unknown('foundation_probe_requires_macos')
    if type(timeout_s) not in (int,float) or not 0<timeout_s<=60:return _unknown('invalid_probe_timeout')
    try:
        with tempfile.TemporaryDirectory(prefix='locua-number-format-') as temp:
            root=Path(temp);source=root/'number-format.swift';source.write_text(_SWIFT);source.chmod(0o600)
            cache=Path(cache_dir) if cache_dir is not None else root/'module-cache'
            cache.mkdir(parents=True,mode=0o700,exist_ok=True)
            response=runner(['/usr/bin/swift','-module-cache-path',str(cache),str(source),bundle_id],
                text=True,capture_output=True,timeout=timeout_s,check=False)
        if response.returncode!=0:return _unknown('foundation_probe_failed',exit_code=response.returncode)
        if len(response.stdout)>65536:return _unknown('foundation_probe_output_too_large')
        return evidence_from_probe(json.loads(response.stdout),bundle_id=bundle_id,target=target)
    except (OSError,ValueError,subprocess.TimeoutExpired):return _unknown('foundation_probe_unavailable')


def parse_display_number(text,evidence,*,expected_target,expected_bundle_id=None,now_ns=None,max_age_s=60):
    """Return one exact rational *under observed locale*, or an explicit unknown.

    There is deliberately no expected-value parameter. Grouping must validate
    before any separator is removed. No locale candidates are tried in search
    of a desired answer. ASCII numerals only; unsupported formats stay unknown.
    """
    if not isinstance(text,str) or not text or len(text)>160:return _unknown('unsupported_numeric_text')
    if not isinstance(evidence,dict) or evidence.get('schema')!=VERSION or evidence.get('status')!='locale_conventions_observed':
        return _unknown('observed_locale_evidence_required')
    body={k:v for k,v in evidence.items() if k!='evidence_sha256'}
    if evidence.get('evidence_sha256')!=_hash(body) or evidence.get('probe_source_sha256')!=PROBE_SHA256:
        return _unknown('locale_evidence_integrity_mismatch')
    if not _target(expected_target) or evidence.get('target')!=expected_target or (expected_bundle_id is not None and evidence.get('bundle_id')!=expected_bundle_id):
        return _unknown('locale_evidence_target_mismatch')
    now=time.time_ns() if now_ns is None else now_ns;captured=evidence.get('observed_at_ns')
    if (type(now) is not int or type(captured) is not int or type(max_age_s) not in (int,float)
            or not 0<max_age_s<=300 or not 0<=now-captured<=max_age_s*1e9):return _unknown('locale_evidence_stale_or_future')
    f=evidence.get('format');error=_validate_format(f)
    if error:return _unknown(error)
    trim_chars=''.join(c for c in ' \t\r\n' if c not in (f['decimal_separator'],f['grouping_separator']))
    trimmed=text.strip(trim_chars);direction_marks=trimmed.count('\u200e')+trimmed.count('\u200f')
    clean=trimmed.replace('\u200e','').replace('\u200f','');unicode_minus='\u2212' in clean
    clean=clean.replace('\u2212','-')
    exponent='';parts=re.split('[eE]',clean)
    if len(parts)>2:return _unknown('invalid_numeric_syntax')
    if len(parts)==2:
        clean,exp=parts
        if not re.fullmatch(r'[+-]?[0-9]{1,3}',exp):return _unknown('invalid_exponent')
        exponent='e'+exp
    sign=''
    if clean[:1] in ('+','-'):sign,clean=clean[0],clean[1:]
    dec,group=f['decimal_separator'],f['grouping_separator']
    chunks=clean.split(dec)
    if len(chunks)>2:return _unknown('multiple_decimal_separators')
    integer=chunks[0];fraction=chunks[1] if len(chunks)==2 else None
    if fraction is not None and fraction and not re.fullmatch('[0-9]+',fraction):return _unknown('invalid_fraction_digits')
    grouped=bool(group and group in integer)
    if grouped:
        if not f['uses_grouping_separator']:return _unknown('grouping_not_enabled_in_observed_conventions')
        groups=integer.split(group);primary=f['primary_grouping_size'];secondary=f['secondary_grouping_size'] or primary
        if (any(not re.fullmatch('[0-9]+',part) for part in groups) or len(groups[-1])!=primary
                or not 1<=len(groups[0])<=secondary or any(len(part)!=secondary for part in groups[1:-1])):
            return _unknown('invalid_grouping')
        integer=''.join(groups)
    if integer and not re.fullmatch('[0-9]+',integer):return _unknown('invalid_integer_digits')
    if not integer and (fraction is None or not fraction):return _unknown('numeric_digits_missing')
    canonical=sign+(integer or '0')+('.'+fraction if fraction is not None else '')+exponent
    try:
        number=Decimal(canonical)
        if not number.is_finite() or abs(number.adjusted())>300:return _unknown('numeric_range_unsupported')
        exact=Fraction(number)
    except (ValueError,InvalidOperation):return _unknown('invalid_numeric_syntax')
    return {'schema':VERSION,'status':'parsed_under_observed_locale','raw':text,'canonical_ascii':canonical,
        'numerator':exact.numerator,'denominator':exact.denominator,'bundle_id':evidence['bundle_id'],
        'target':deepcopy(expected_target),'locale_identifier':f['locale_identifier'],
        'locale_evidence_sha256':evidence['evidence_sha256'],'application_formatter_proven':False,
        'normalization':{'direction_marks_removed':direction_marks,'unicode_minus_normalized':unicode_minus,
            'outer_ascii_whitespace_removed':trimmed!=text,'grouping_validated':grouped},
        'limitations':deepcopy(evidence['limitations'])}
