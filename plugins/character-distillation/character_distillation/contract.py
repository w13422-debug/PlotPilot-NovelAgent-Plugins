"""Closed business contracts for Character Distillation (source-only)."""
from __future__ import annotations
from copy import deepcopy
import hashlib, json, re
from pathlib import Path
from typing import Any, Mapping, Sequence
try:
 from jsonschema import Draft202012Validator
except ImportError: Draft202012Validator=None
try:
 from plotpilot_plugin_sdk.canonical import canonical_bytes as _canonical_bytes
except ImportError: _canonical_bytes=None
HASH_RE=re.compile(r'^[0-9a-f]{64}$'); ID_RE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')
class CharacterContractError(ValueError):pass
class EvidenceSpanError(CharacterContractError):pass
class AuthorityError(CharacterContractError):pass

def _closed(value:Any,fields:set[str]|frozenset[str],label:str)->dict[str,Any]:
 if not isinstance(value,Mapping):raise CharacterContractError(f'{label} must be an object')
 value=dict(value); actual=set(value); expected=set(fields)
 if actual!=expected:raise CharacterContractError(f'{label} fields are not closed: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}')
 return value

def _id(value:Any,label:str)->str:
 if not isinstance(value,str) or ID_RE.fullmatch(value) is None:raise CharacterContractError(f'{label} must be a Core identifier')
 return value

def _hash(value:Any,label:str)->str:
 if not isinstance(value,str) or HASH_RE.fullmatch(value) is None:raise CharacterContractError(f'{label} must be lowercase SHA-256')
 return value

def _text(value:Any,label:str,allow_empty:bool=False)->str:
 if not isinstance(value,str) or (not allow_empty and not value):raise CharacterContractError(f'{label} must be a string')
 try:value.encode('utf8','strict')
 except UnicodeEncodeError as exc:raise CharacterContractError(f'{label} contains surrogate') from exc
 return value

def _int(value:Any,label:str,min_value:int=0)->int:
 if isinstance(value,bool) or not isinstance(value,int) or value<min_value:raise CharacterContractError(f'{label} must be integer >= {min_value}')
 return value

def canonical_json_bytes(value:Any)->bytes:
 if _canonical_bytes is not None:return bytes(_canonical_bytes(value))
 return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
def hash_json(schema:str,value:Any)->str:return hashlib.sha256(schema.encode('ascii')+b'\n'+canonical_json_bytes(value)).hexdigest()
def sha256_text(value:str)->str:
 return hashlib.sha256(_text(value,'text',True).encode('utf8')).hexdigest()

def validate_nodes(nodes:Any,text_length:int)->list[dict[str,Any]]:
 if not isinstance(nodes,list) or not nodes:raise CharacterContractError('nodes must be non-empty')
 result=[]; seen=set()
 for i,raw in enumerate(nodes):
  node=_closed(raw,{'node_id','start_codepoint','end_codepoint'},f'nodes[{i}]'); nid=_id(node['node_id'],f'nodes[{i}].node_id'); start=_int(node['start_codepoint'],f'nodes[{i}].start_codepoint');end=_int(node['end_codepoint'],f'nodes[{i}].end_codepoint',1)
  if nid in seen or start>=end or end>text_length:raise CharacterContractError(f'nodes[{i}] has invalid bounds or duplicate identity')
  seen.add(nid);result.append({'node_id':nid,'start_codepoint':start,'end_codepoint':end})
 return result
EVIDENCE_FIELDS=frozenset({'schema','workspace_id','document_id','revision_id','node_id','start_codepoint','end_codepoint','quote','quote_hash','canonical_text_hash'})
def build_evidence_span(*,workspace_id:str,document_id:str,revision_id:str,node_id:str,start_codepoint:int,end_codepoint:int,canonical_text:str,node_range:Mapping[str,int]|None=None)->dict[str,Any]:
 text=_text(canonical_text,'canonical_text',True);start=_int(start_codepoint,'start_codepoint');end=_int(end_codepoint,'end_codepoint',1)
 if start>=end or end>len(text):raise EvidenceSpanError('EvidenceSpan bounds exceed canonical Revision')
 if node_range is not None:
  ns=_int(node_range.get('start_codepoint'),'node start');ne=_int(node_range.get('end_codepoint'),'node end',1)
  if start<ns or end>ne:raise EvidenceSpanError('EvidenceSpan is outside Node range')
 quote=text[start:end]
 return {'schema':'evidence-span/v1','workspace_id':_id(workspace_id,'workspace_id'),'document_id':_id(document_id,'document_id'),'revision_id':_id(revision_id,'revision_id'),'node_id':_id(node_id,'node_id'),'start_codepoint':start,'end_codepoint':end,'quote':quote,'quote_hash':sha256_text(quote),'canonical_text_hash':sha256_text(text)}
def validate_evidence_span(raw:Any,canonical_text:str,nodes:Sequence[Mapping[str,Any]],*,workspace_id:str,document_id:str,revision_id:str,canonical_text_hash:str)->dict[str,Any]:
 span=_closed(raw,EVIDENCE_FIELDS,'EvidenceSpan')
 if span['schema']!='evidence-span/v1':raise EvidenceSpanError('EvidenceSpan schema mismatch')
 for f,expected in [('workspace_id',workspace_id),('document_id',document_id),('revision_id',revision_id)]:
  if _id(span[f],f)!=expected:raise EvidenceSpanError(f'EvidenceSpan {f} mismatch')
 node_id=_id(span['node_id'],'node_id'); text=_text(canonical_text,'canonical_text',True); nodes_v=validate_nodes(nodes,len(text)); node=next((n for n in nodes_v if n['node_id']==node_id),None)
 if node is None:raise EvidenceSpanError('EvidenceSpan node is unknown')
 start=_int(span['start_codepoint'],'start_codepoint');end=_int(span['end_codepoint'],'end_codepoint',1)
 if start>=end or end>len(text) or start<node['start_codepoint'] or end>node['end_codepoint']:raise EvidenceSpanError('EvidenceSpan range is outside Node/canonical text')
 quote=_text(span['quote'],'quote',True)
 if quote!=text[start:end]:raise EvidenceSpanError('EvidenceSpan quote is not canonical scalar slice')
 if _hash(span['quote_hash'],'quote_hash')!=sha256_text(quote):raise EvidenceSpanError('EvidenceSpan quote_hash mismatch')
 if _hash(span['canonical_text_hash'],'canonical_text_hash')!=canonical_text_hash or span['canonical_text_hash']!=sha256_text(text):raise EvidenceSpanError('EvidenceSpan canonical_text_hash mismatch')
 return deepcopy(span)

def validate_archetype(raw:Any)->dict[str,Any]:
 if not isinstance(raw,Mapping):raise CharacterContractError('character-archetype/v1 must be an object')
 value=dict(raw)
 schema_path=Path(__file__).resolve().parent/'schemas'/'character-archetype-v1.schema.json'
 try:
  schema=json.loads(schema_path.read_text(encoding='utf8'))
  if Draft202012Validator is None:raise CharacterContractError('frozen character-archetype schema verifier unavailable')
  errors=sorted(Draft202012Validator(schema).iter_errors(value),key=lambda e:list(e.absolute_path))
  if errors:raise CharacterContractError('character-archetype/v1 schema: '+errors[0].message)
 except FileNotFoundError as exc:raise CharacterContractError('frozen character-archetype schema unavailable') from exc
 if value.get('schema')!='character-archetype/v1' or value.get('format_id')!='character-archetype/v1':raise CharacterContractError('character archetype identity mismatch')
 dims=value['dimensions']; dim_ids=[d['dimension_id'] for d in dims]
 if len(dim_ids)!=len(set(dim_ids)) or not all(d['dimension_id'] for d in dims):raise CharacterContractError('archetype dimensions are not unique')
 # The Data package is frozen against the catalog's exact bidirectional
 # interpreter set.  A merely shape-valid mapping list could otherwise make a
 # foreign/partial archetype appear authoritative to the distiller.
 expected_pairs=(
  ('com.plotpilot.novelagent.character-distillation','analysis.character.atom.extract/v1'),
  ('com.plotpilot.novelagent.character-distillation','analysis.character.card.generate/v1'),
  ('com.plotpilot.novelagent.character-distillation','analysis.character.conflict.review/v1'),
  ('com.plotpilot.novelagent.character-distillation','analysis.character.conflict.apply/v1'),
  ('com.plotpilot.novelagent.asset-derivation','asset.template.derive/v1'),
  ('com.plotpilot.novelagent.writing-context','writing.context.assemble/v1'),
  ('com.plotpilot.novelagent.chapter-workflow','writing.chapter.draft/v1'),
 )
 mappings=value.get('interpreter_mappings')
 expected_mappings=[{'format_id':'character-archetype/v1','plugin_id':plugin,'capability_id':cap,'direction':direction} for plugin,cap in expected_pairs for direction in ('format_to_capability','capability_to_format')]
 if mappings!=expected_mappings:raise CharacterContractError('archetype interpreter mappings are not the frozen exact bidirectional set')
 return deepcopy(value)

def _provenance_fields(value:Mapping[str,Any])->dict[str,Any]:
 p=_closed(value,{'plugin_id','capability_id','package_hash','release_id','run_snapshot_hash','model_receipt_id'},'provenance')
 _id(p['plugin_id'],'provenance.plugin_id');_id(p['capability_id'],'provenance.capability_id')
 for f in ('package_hash','release_id','run_snapshot_hash'):_hash(p[f],f)
 if p['model_receipt_id'] is not None:_id(p['model_receipt_id'],'model_receipt_id')
 return p
ATOM_FIELDS=frozenset({'schema','atom_id','character_id','dimension','kind','observation','interpretation','confidence','source_revision_id','evidence_spans','provenance','authority'})
def validate_character_atom(raw:Any,*,archetype:Mapping[str,Any],canonical_text:str,nodes:Sequence[Mapping[str,Any]],workspace_id:str,document_id:str,revision_id:str,canonical_text_hash:str,expected_provenance:Mapping[str,Any]|None=None)->dict[str,Any]:
 value=_closed(raw,ATOM_FIELDS,'Character Atom')
 if value['schema']!='character-atom/v1' or value['authority']!='candidate_only':raise AuthorityError('Character Atom must remain candidate_only')
 for f in ('atom_id','character_id','dimension','kind','source_revision_id'):_id(value[f],f)
 if value['source_revision_id']!=revision_id:raise CharacterContractError('Atom source Revision mismatch')
 dim_ids={d['dimension_id'] for d in archetype['dimensions']};kind_ids=set(archetype['atom_kinds'])
 if value['dimension'] not in dim_ids or value['kind'] not in kind_ids:raise CharacterContractError('Atom dimension/kind is not declared by archetype')
 for f in ('observation','interpretation'):_text(value[f],f)
 if value['confidence'] not in {'confirmed','inferred','uncertain','conflict'}:raise CharacterContractError('Atom confidence invalid')
 if not isinstance(value['evidence_spans'],list) or not value['evidence_spans']:raise CharacterContractError('Atom evidence is required')
 value['evidence_spans']=[validate_evidence_span(s,canonical_text,nodes,workspace_id=workspace_id,document_id=document_id,revision_id=revision_id,canonical_text_hash=canonical_text_hash) for s in value['evidence_spans']]
 p=_provenance_fields(value['provenance'])
 if expected_provenance is not None and p!=dict(expected_provenance):raise CharacterContractError('Atom provenance does not equal trusted run binding')
 value['provenance']=p;return deepcopy(value)
CARD_FIELDS=frozenset({'schema','character_id','name','identity','goals','motivations','fears','abilities','flaws','relationships','conflicts','arc','speech_habits','behavior_evidence','source_revision_id','source_refs','authority','provenance'})
def validate_character_card(raw:Any,*,archetype:Mapping[str,Any],canonical_text:str,nodes:Sequence[Mapping[str,Any]],workspace_id:str,document_id:str,revision_id:str,canonical_text_hash:str,expected_provenance:Mapping[str,Any]|None=None)->dict[str,Any]:
 v=_closed(raw,CARD_FIELDS,'Character Card')
 if v['schema']!='character-card/v1' or v['authority']!='candidate_only':raise AuthorityError('Character Card authority is not candidate_only')
 if v['source_revision_id']!=revision_id:raise CharacterContractError('Card Revision mismatch')
 for f in ('character_id','source_revision_id'):_id(v[f],f)
 for f in ('name','identity'): _text(v[f],f)
 for f in ('goals','motivations','fears','abilities','flaws','conflicts','speech_habits'):
  if not isinstance(v[f],list) or not v[f] or any(not isinstance(x,str) or not x for x in v[f]):raise CharacterContractError(f'Card {f} must be non-empty strings')
 if not isinstance(v['relationships'],list):raise CharacterContractError('Card relationships must be array')
 rel_kinds=set(archetype['relation_kinds']);
 for i,r in enumerate(v['relationships']):
  r=_closed(r,{'relation_id','kind','from_character_id','to_character_id','label','evidence_spans'},f'relationships[{i}]');_id(r['relation_id'],'relation_id');_id(r['from_character_id'],'from_character_id');_id(r['to_character_id'],'to_character_id');_text(r['label'],'label')
  if r['kind'] not in rel_kinds:raise CharacterContractError('Card relationship kind not declared')
  r['evidence_spans']=[validate_evidence_span(s,canonical_text,nodes,workspace_id=workspace_id,document_id=document_id,revision_id=revision_id,canonical_text_hash=canonical_text_hash) for s in r['evidence_spans']]
 if set(v['arc'])!={'stage','stages','summary','key_events'}:raise CharacterContractError('Card arc fields are not closed')
 if v['arc']['stage'] not in set(archetype['arc_stages']) or any(s not in set(archetype['arc_stages']) for s in v['arc']['stages']):raise CharacterContractError('Card arc stage invalid')
 _text(v['arc']['summary'],'arc.summary');
 if not isinstance(v['arc']['key_events'],list) or not v['arc']['key_events']:raise CharacterContractError('Card arc key_events required')
 if not isinstance(v['behavior_evidence'],list) or not v['behavior_evidence']:raise CharacterContractError('Card behavior evidence required')
 v['behavior_evidence']=[validate_evidence_span(s,canonical_text,nodes,workspace_id=workspace_id,document_id=document_id,revision_id=revision_id,canonical_text_hash=canonical_text_hash) for s in v['behavior_evidence']]
 if not isinstance(v['source_refs'],list) or not v['source_refs']:raise CharacterContractError('Card source_refs required')
 for i,r in enumerate(v['source_refs']):
  r=_closed(r,{'workspace_id','document_id','revision_id'},f'source_refs[{i}]')
  if r!={'workspace_id':workspace_id,'document_id':document_id,'revision_id':revision_id}:raise CharacterContractError('Card source reference mismatch')
 v['provenance']=_provenance_fields(v['provenance'])
 if expected_provenance is not None and v['provenance']!=dict(expected_provenance):raise CharacterContractError('Card provenance drift')
 return deepcopy(v)
CONFLICT_FIELDS=frozenset({'schema','conflict_id','character_id','field','values','authority'})
def validate_conflict(raw:Any,*,canonical_text:str,nodes:Sequence[Mapping[str,Any]],workspace_id:str,document_id:str,revision_id:str,canonical_text_hash:str)->dict[str,Any]:
 v=_closed(raw,CONFLICT_FIELDS,'Character Conflict')
 if v['schema']!='character-conflict/v1' or v['authority']!='diagnostic_only':raise AuthorityError('Conflict diagnostics cannot claim truth authority')
 for f in ('conflict_id','character_id','field'):_id(v[f],f)
 if not isinstance(v['values'],list) or len(v['values'])<2:raise CharacterContractError('Conflict requires at least two source values')
 seen=set()
 normalized_values=[]
 for i,item in enumerate(v['values']):
  item=_closed(item,{'source_id','value','evidence_spans'},f'conflict.values[{i}]');sid=_id(item['source_id'],'source_id')
  if sid in seen:raise CharacterContractError('Conflict source IDs duplicate')
  seen.add(sid)
  if not isinstance(item['evidence_spans'],list) or not item['evidence_spans']:raise CharacterContractError('Conflict evidence required')
  item['evidence_spans']=[validate_evidence_span(s,canonical_text,nodes,workspace_id=workspace_id,document_id=document_id,revision_id=revision_id,canonical_text_hash=canonical_text_hash) for s in item['evidence_spans']]
  normalized_values.append(item)
 v['values']=normalized_values
 return deepcopy(v)
RULING_FIELDS=frozenset({'schema','conflict_id','selected_source_ids','decision','rationale','actor_id','created_at'})
def validate_ruling(raw:Any,conflict:Mapping[str,Any])->dict[str,Any]:
 v=_closed(raw,RULING_FIELDS,'Conflict ruling')
 if v['schema']!='character-conflict-ruling/v1':raise CharacterContractError('ruling schema mismatch')
 if v['conflict_id']!=conflict['conflict_id'] or not isinstance(v['selected_source_ids'],list) or not v['selected_source_ids']:raise CharacterContractError('ruling conflict/selection mismatch')
 source_ids={x['source_id'] for x in conflict['values']}
 if len(v['selected_source_ids'])!=len(set(v['selected_source_ids'])) or not set(v['selected_source_ids'])<=source_ids:raise CharacterContractError('ruling selects unknown/duplicate source')
 if v['decision'] not in {'select','reject','defer'}:raise CharacterContractError('ruling decision invalid')
 _text(v['rationale'],'rationale');_id(v['actor_id'],'actor_id');_text(v['created_at'],'created_at')
 if not re.fullmatch(r'^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$',v['created_at']):raise CharacterContractError('ruling created_at has invalid UTC form')
 return deepcopy(v)
def apply_conflict_ruling(conflict:Mapping[str,Any],ruling:Mapping[str,Any])->dict[str,Any]:
 r=validate_ruling(ruling,conflict)
 if r['decision']!='select':raise CharacterContractError('only explicit select ruling may produce Candidate')
 selected=set(r['selected_source_ids']); values=[x for x in conflict['values'] if x['source_id'] in selected]
 if len(values)!=len(selected):raise CharacterContractError('ruling selection changed')
 if len(values)<len(conflict['values']):
  selected_values={json.dumps(x['value'],ensure_ascii=False,sort_keys=True,separators=(',',':')) for x in values}
  all_values={json.dumps(x['value'],ensure_ascii=False,sort_keys=True,separators=(',',':')) for x in conflict['values']}
  if selected_values==all_values:raise CharacterContractError('explicit ruling would narrow provenance without new content')
 return {'conflict_id':conflict['conflict_id'],'field':conflict['field'],'selected_sources':deepcopy(values),'ruling':r,'authority':'candidate_only'}

def validate_source_narrowing(previous:Mapping[str,Any],current:Mapping[str,Any],*,value_fields:Sequence[str]=('value','state','change','observation','interpretation'),source_field:str='source_ids')->dict[str,Any]:
 """Reject unchanged values whose source set shrinks, and syntheses without new content."""
 old_raw=previous.get(source_field,previous.get('source_refs',[])) or [];new_raw=current.get(source_field,current.get('source_refs',[])) or []
 try:old_sources={json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':')) if isinstance(x,Mapping) else str(x) for x in old_raw};new_sources={json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':')) if isinstance(x,Mapping) else str(x) for x in new_raw}
 except Exception as exc:raise CharacterContractError('source provenance set is not serializable') from exc
 for field in value_fields:
  if field in previous and field in current and previous[field]==current[field] and new_sources<old_sources:raise CharacterContractError('source narrowing changed provenance without changing value')
 if previous.get('value')==current.get('value') and previous.get('state')==current.get('state') and previous.get('change')==current.get('change') and new_sources<=old_sources:
  raise CharacterContractError('synthesize has no new content or source')
 return deepcopy(dict(current))

def source_closure(values:Sequence[Mapping[str,Any]],*,accepted_current_ids:set[str],source_revision_id:str)->None:
 for item in values:
  if item.get('source_revision_id')!=source_revision_id or item.get('atom_id') not in accepted_current_ids:raise CharacterContractError('character source closure is not accepted/current')
__all__=['CharacterContractError','EvidenceSpanError','AuthorityError','build_evidence_span','validate_evidence_span','validate_nodes','validate_archetype','validate_character_atom','validate_character_card','validate_conflict','validate_ruling','apply_conflict_ruling','validate_source_narrowing','source_closure','sha256_text','hash_json','canonical_json_bytes']
