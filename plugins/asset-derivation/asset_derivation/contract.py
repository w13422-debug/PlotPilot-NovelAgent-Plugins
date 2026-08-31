"""Strict target-format and canonical Data-bundle contracts for NAP-04."""
from __future__ import annotations
from copy import deepcopy
import hashlib,json,re,sys
from pathlib import Path
from typing import Any,Mapping,Sequence
try: from jsonschema import Draft202012Validator
except ImportError: Draft202012Validator=None
try:
 from plotpilot_plugin_sdk.canonical import canonical_bytes
except ImportError: canonical_bytes=None
try:
 from plotpilot_plugin_sdk.package import digest_package,release_id as sdk_release_id
except ImportError: digest_package=sdk_release_id=None
try:
 from plotpilot_plugin_sdk.package import normalize_relative_path
 from plotpilot_plugin_sdk.package import unicode_nfc_casefold
except ImportError:
 normalize_relative_path=unicode_nfc_casefold=None
HASH_RE=re.compile(r'^[0-9a-f]{64}$');ID_RE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')
class AssetContractError(ValueError):pass
class TargetSchemaError(AssetContractError):pass
class DataBundleError(AssetContractError):pass
TARGET_SCHEMA_FILES={'character-archetype/v1':'character-archetype-v1.schema.json','world-rule-template/v1':'world-rule-template-v1.schema.json','plot-structure-template/v1':'plot-structure-template-v1.schema.json','character-card/v1':'character-card-v1.schema.json','world-entry/v1':'world-entry-v1.schema.json','world-entry/world':'world-entry-v1.schema.json','world/v1':'world-entry-v1.schema.json'}
NAP04_INTERPRETER_MAP={
 'character-archetype/v1':(('com.plotpilot.novelagent.character-distillation','analysis.character.atom.extract/v1'),('com.plotpilot.novelagent.character-distillation','analysis.character.card.generate/v1'),('com.plotpilot.novelagent.character-distillation','analysis.character.conflict.review/v1'),('com.plotpilot.novelagent.character-distillation','analysis.character.conflict.apply/v1'),('com.plotpilot.novelagent.asset-derivation','asset.template.derive/v1'),('com.plotpilot.novelagent.writing-context','writing.context.assemble/v1'),('com.plotpilot.novelagent.chapter-workflow','writing.chapter.draft/v1')),
 'world-rule-template/v1':(('com.plotpilot.novelagent.asset-derivation','asset.template.derive/v1'),('com.plotpilot.novelagent.consistency-audit','quality.consistency.audit/v1'),('com.plotpilot.novelagent.story-state','story.chapter.settle/v1'),('com.plotpilot.novelagent.story-state','story.state.derive/v1'),('com.plotpilot.novelagent.writing-context','writing.context.assemble/v1'),('com.plotpilot.novelagent.chapter-workflow','writing.chapter.draft/v1')),
 'plot-structure-template/v1':(('com.plotpilot.novelagent.narrative-analysis','analysis.narrative.unit.extract/v1'),('com.plotpilot.novelagent.narrative-analysis','analysis.narrative.plan.compile/v1'),('com.plotpilot.novelagent.narrative-analysis','analysis.narrative.synthesize/v1'),('com.plotpilot.novelagent.asset-derivation','asset.template.derive/v1'),('com.plotpilot.novelagent.writing-context','writing.context.assemble/v1'),('com.plotpilot.novelagent.chapter-workflow','writing.project.outline/v1'),('com.plotpilot.novelagent.chapter-workflow','writing.volume.outline/v1'),('com.plotpilot.novelagent.chapter-workflow','writing.chapter.outline/v1')),
}
DATA_PLUGIN_BY_FORMAT={
 'character-archetype/v1':'com.plotpilot.novelagent.character-archetype',
 'world-rule-template/v1':'com.plotpilot.novelagent.world-rule-template',
}
def _id(v,label):
 if not isinstance(v,str) or ID_RE.fullmatch(v) is None:raise AssetContractError(f'{label} must be a Core identifier')
 return v
def _hash(v,label):
 if not isinstance(v,str) or HASH_RE.fullmatch(v) is None:raise AssetContractError(f'{label} must be lowercase SHA-256')
 return v
def _text(v,label):
 if not isinstance(v,str) or not v:raise AssetContractError(f'{label} must be non-empty text')
 return v
def canonical_json_bytes(value:Any)->bytes:
 if canonical_bytes is not None:return bytes(canonical_bytes(value))
 return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
def hash_json(schema:str,value:Any)->str:return hashlib.sha256(schema.encode('ascii')+b'\n'+canonical_json_bytes(value)).hexdigest()
def _schema(format_id:str)->dict[str,Any]:
 name=TARGET_SCHEMA_FILES.get(format_id)
 if not name:raise TargetSchemaError(f'no frozen closed schema for target format {format_id}')
 path=Path(__file__).resolve().parent/'schemas'/'targets'/name
 try:return json.loads(path.read_text(encoding='utf8'))
 except Exception as exc:raise TargetSchemaError(f'frozen target schema unavailable for {format_id}') from exc
def validate_target_payload(format_id:str,payload:Any)->dict[str,Any]:
 """Load and semantically validate a target; unknown formats never degrade to permissive."""
 if not isinstance(payload,Mapping):raise TargetSchemaError('target payload must be object')
 value=dict(payload); schema=_schema(format_id)
 if Draft202012Validator is None:raise TargetSchemaError('jsonschema verifier unavailable')
 errors=sorted(Draft202012Validator(schema).iter_errors(value),key=lambda e:list(e.absolute_path))
 if errors:raise TargetSchemaError(f'{format_id} schema: {errors[0].message}')
 # Every target alias is a distinct frozen format identity.  The shared
 # world-entry schema intentionally enumerates the three historical aliases
 # so it can be loaded from one artifact, but accepting an alias different
 # from the request would let a foreign format cross the Candidate boundary.
 # Enforce both identity fields against the requested target after structural
 # JSON-Schema validation.
 if value.get('schema')!=format_id or value.get('format_id')!=format_id:raise TargetSchemaError('target format identity mismatch')
 if format_id=='character-archetype/v1':
  dims=value.get('dimensions');
  if not isinstance(dims,list) or not dims or len({d['dimension_id'] for d in dims})!=len(dims):raise TargetSchemaError('character archetype dimensions are not closed/unique')
  if any(d['required'] and not d['description'] for d in dims):raise TargetSchemaError('required character dimension lacks semantic description')
  maps=value.get('interpreter_mappings',[]);verify_interpreter_mappings(maps,format_id=format_id)
 elif format_id=='world-rule-template/v1':
  rules=value.get('rules',[])
  if len({r['rule_id'] for r in rules})!=len(rules) or any(r['priority']<0 for r in rules):raise TargetSchemaError('world rules have duplicate/invalid priority')
  verify_interpreter_mappings(value.get('interpreter_mappings',[]),format_id=format_id)
 elif format_id=='plot-structure-template/v1':
  beats=value.get('beats',[]); ordinals=[b['ordinal'] for b in beats]
  if ordinals!=list(range(1,len(beats)+1)) or len({b['beat_id'] for b in beats})!=len(beats):raise TargetSchemaError('plot beats must have continuous unique ordinals')
  ids={b['beat_id'] for b in beats}
  if any(child not in ids for b in beats for child in b['children']):raise TargetSchemaError('plot beat child reference is unknown')
 elif format_id=='character-card/v1':
  if value.get('authority')!='candidate_only':raise TargetSchemaError('character-card target cannot claim publication authority')
  # The target schema is intentionally closed, but these semantic checks are
  # not expressible in JSON Schema.  A derived card must carry real source
  # closure and internally coherent evidence rather than a shape-only stub.
  if not value.get('behavior_evidence') or not value.get('source_refs'):
   raise TargetSchemaError('character-card target requires behavior evidence and source refs')
  for i,span in enumerate(value['behavior_evidence']):
   _validate_target_span(span,f'character-card behavior_evidence[{i}]')
  for i,ref in enumerate(value['source_refs']):
   _validate_target_source_ref(ref,f'character-card source_refs[{i}]')
  for i,rel in enumerate(value.get('relationships',[])):
   if not rel.get('evidence_spans'):
    raise TargetSchemaError(f'character-card relationship[{i}] requires evidence')
   for j,span in enumerate(rel['evidence_spans']):
    _validate_target_span(span,f'character-card relationship[{i}].evidence_spans[{j}]')
 elif format_id in {'world-entry/v1','world-entry/world','world/v1'}:
  if not value.get('evidence_spans') or not value.get('source_refs'):raise TargetSchemaError('world entry requires evidence/source closure')
  for i,span in enumerate(value['evidence_spans']):
   _validate_target_span(span,f'{format_id} evidence_spans[{i}]')
  for i,ref in enumerate(value['source_refs']):
   _validate_target_source_ref(ref,f'{format_id} source_refs[{i}]')
 return deepcopy(value)

def _validate_target_span(span:Any,label:str)->None:
 """Validate the self-contained portion of an EvidenceSpan.

 The source-bound character worker performs the full canonical-text/node
 check.  Target Data payloads do not carry the complete source text, so this
 gate still enforces the non-permissive invariants that are locally
 recomputable: exact closed fields, scalar range/quote length and both
 content hashes.
 """
 if not isinstance(span,Mapping) or set(span)!={'schema','workspace_id','document_id','revision_id','node_id','start_codepoint','end_codepoint','quote','quote_hash','canonical_text_hash'}:
  raise TargetSchemaError(f'{label} is not a closed EvidenceSpan')
 if span['schema']!='evidence-span/v1':raise TargetSchemaError(f'{label} schema mismatch')
 for name in ('workspace_id','document_id','revision_id','node_id'):_id(span[name],f'{label}.{name}')
 start,end=span['start_codepoint'],span['end_codepoint']
 if isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,int) or not isinstance(end,int) or start<0 or end<=start:
  raise TargetSchemaError(f'{label} range is invalid')
 if not isinstance(span['quote'],str) or len(span['quote'])!=end-start:
  raise TargetSchemaError(f'{label} quote/range mismatch')
 _hash(span['quote_hash'],f'{label}.quote_hash');_hash(span['canonical_text_hash'],f'{label}.canonical_text_hash')
 if hashlib.sha256(span['quote'].encode('utf8')).hexdigest()!=span['quote_hash']:
  raise TargetSchemaError(f'{label} quote_hash mismatch')

def _validate_target_source_ref(ref:Any,label:str)->None:
 if not isinstance(ref,Mapping) or set(ref)!={'workspace_id','document_id','revision_id'}:
  raise TargetSchemaError(f'{label} is not a closed source reference')
 for name in ('workspace_id','document_id','revision_id'):_id(ref[name],f'{label}.{name}')

def expected_interpreter_mappings(format_id:str)->list[dict[str,str]]:
 pairs=NAP04_INTERPRETER_MAP.get(format_id)
 if pairs is None:raise DataBundleError(f'unknown frozen interpreter format {format_id}')
 return [{'format_id':format_id,'plugin_id':plugin,'capability_id':cap,'direction':direction} for plugin,cap in pairs for direction in ('format_to_capability','capability_to_format')]
def verify_interpreter_mappings(mappings:Any,*,format_id:str|None=None)->list[dict[str,str]]:
 if not isinstance(mappings,list):raise DataBundleError('interpreter mappings must be an array')
 if any(not isinstance(item,Mapping) for item in mappings):raise DataBundleError('interpreter mappings must contain objects')
 if format_id is None:
  formats={m.get('format_id') for m in mappings}
  if len(formats)!=1:raise DataBundleError('mapping format must be singular')
  format_id=next(iter(formats))
 expected=expected_interpreter_mappings(format_id)
 if mappings!=expected:
  # Compare set separately so order/duplicate/omission/foreign mappings all fail closed.
  if len(mappings)!=len(expected) or set(json.dumps(x,sort_keys=True) for x in mappings)!=set(json.dumps(x,sort_keys=True) for x in expected):raise DataBundleError(f'interpreter mapping set is not exact for {format_id}')
  raise DataBundleError('interpreter mapping order is not canonical')
 return deepcopy(mappings)

def independent_package_identity(files:Mapping[str,bytes],plugin_id:str,version:str):
 if digest_package is None:raise DataBundleError('public SDK package identity unavailable')
 return digest_package(files,plugin_id,version)
def build_data_bundle(*,data_plugin_id:str,data_release_id:str,package_hash:str,format_id:str,root_path:str,files:Sequence[Mapping[str,Any]])->dict[str,Any]:
 _id(data_plugin_id,'data_plugin_id');_hash(data_release_id,'data_release_id');_hash(package_hash,'package_hash');_text(format_id,'format_id');_text(root_path,'root_path')
 if format_id not in NAP04_INTERPRETER_MAP:raise DataBundleError(f'unknown frozen Data format {format_id}')
 expected_plugin=DATA_PLUGIN_BY_FORMAT.get(format_id)
 if expected_plugin is None or data_plugin_id!=expected_plugin:raise DataBundleError('data_plugin_id does not own the frozen format')
 if normalize_relative_path is None:raise DataBundleError('public SDK path verifier unavailable')
 try:canonical_root=normalize_relative_path(root_path)
 except Exception as exc:raise DataBundleError(f'root_path is not a canonical relative path: {exc}') from exc
 rows=[];seen_assets=set();seen_paths=set()
 for index,raw in enumerate(files):
  if not isinstance(raw,Mapping) or set(raw)!={'path','asset_id','sha256','mime','size'}:raise DataBundleError(f'file[{index}] fields are not closed')
  row=dict(raw)
  try:path=normalize_relative_path(row['path'])
  except Exception as exc:raise DataBundleError(f'file[{index}] path is invalid: {exc}') from exc
  if path!=row['path']:raise DataBundleError(f'file[{index}] path is not normalized')
  _id(row['asset_id'],f'file[{index}].asset_id');_hash(row['sha256'],f'file[{index}].sha256');_int_size=row.get('size')
  if isinstance(_int_size,bool) or not isinstance(_int_size,int) or _int_size<0:raise DataBundleError(f'file[{index}].size is invalid')
  _text(row['mime'],f'file[{index}].mime')
  folded=unicode_nfc_casefold(path) if unicode_nfc_casefold is not None else path.casefold()
  if folded in seen_paths:raise DataBundleError(f'file path collision: {path}')
  if row['asset_id'] in seen_assets:raise DataBundleError('file Asset IDs must be unique')
  seen_paths.add(folded);seen_assets.add(row['asset_id']);rows.append(row)
 if not rows:raise DataBundleError('Data bundle requires at least one file')
 # canonical path order and duplicate/casefold defenses are delegated to SDK verifier too.
 rows.sort(key=lambda x:x['path'].encode('utf8'))
 if canonical_root not in {x['path'] for x in rows}:raise DataBundleError('root_path is absent from files')
 base={'schema':'plugin-data-bundle/v1','bundle_id':f'{data_plugin_id.rsplit(".",1)[-1]}-bundle','data_plugin_id':data_plugin_id,'data_release_id':data_release_id,'package_hash':package_hash,'format_id':format_id,'root_path':canonical_root,'files':rows}
 bundle={**base,'bundle_hash':hashlib.sha256(b'plugin-data-bundle/v1\n'+canonical_json_bytes(base)).hexdigest()}
 try:
  from plotpilot_plugin_sdk.verifier import verify_data_bundle
  verify_data_bundle(bundle)
 except Exception as exc:raise DataBundleError(f'plugin-data-bundle verifier rejected bundle: {exc}') from exc
 return bundle
def verify_data_bundle_identity(bundle:Mapping[str,Any],*,version:str,files_by_path:Mapping[str,bytes]|None=None)->None:
 try:
  from plotpilot_plugin_sdk.verifier import verify_data_bundle
  verify_data_bundle(bundle)
 except Exception as exc:raise DataBundleError(str(exc)) from exc
 if files_by_path is None:raise DataBundleError('bundle identity verification requires raw file bytes')
 if not isinstance(files_by_path,Mapping):raise DataBundleError('bundle raw files must be a path-to-bytes mapping')
 listed_paths={row['path'] for row in bundle['files']}
 actual_paths=set(files_by_path)
 if actual_paths!=listed_paths:
  raise DataBundleError('bundle raw files must exactly equal the declared file table')
 for row in bundle['files']:
  path=row['path'];data=files_by_path.get(path)
  if data is None or hashlib.sha256(data).hexdigest()!=row['sha256'] or len(data)!=row['size']:raise DataBundleError(f'bundle file bytes mismatch: {path}')
 if digest_package is None:raise DataBundleError('SDK package identity unavailable')
 try:
  recomputed=digest_package(files_by_path,bundle['data_plugin_id'],version)
 except Exception as exc:raise DataBundleError(f'SDK package identity recomputation failed: {exc}') from exc
 if recomputed.package_hash!=bundle['package_hash'] or recomputed.release_id!=bundle['data_release_id']:
  raise DataBundleError('bundle package/release identity does not match SDK recomputation')
 if sdk_release_id is None:raise DataBundleError('SDK release identity unavailable')
 expected=sdk_release_id(bundle['data_plugin_id'],version,bundle['package_hash'])
 if bundle['data_release_id']!=expected:raise DataBundleError('data_release_id does not match SDK canonical release identity')
 unsigned={k:v for k,v in bundle.items() if k!='bundle_hash'}
 if bundle['bundle_hash']!=hashlib.sha256(b'plugin-data-bundle/v1\n'+canonical_json_bytes(unsigned)).hexdigest():raise DataBundleError('bundle_hash mismatch')
__all__=['AssetContractError','TargetSchemaError','DataBundleError','TARGET_SCHEMA_FILES','NAP04_INTERPRETER_MAP','DATA_PLUGIN_BY_FORMAT','validate_target_payload','expected_interpreter_mappings','verify_interpreter_mappings','independent_package_identity','build_data_bundle','verify_data_bundle_identity','canonical_json_bytes','hash_json']
