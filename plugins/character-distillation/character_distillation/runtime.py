"""Host-bound Character Distillation worker with fail-closed source/result gates."""
from __future__ import annotations
import base64,hashlib,json,re,threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any,Mapping,Protocol
from .capability_spec import CAPABILITIES,NEEDS,PLUGIN_ID,SPEC_BY_CAPABILITY,VERSION,descriptors
from .contract import CharacterContractError,apply_conflict_ruling,build_evidence_span,canonical_json_bytes,hash_json,sha256_text,validate_archetype,validate_character_atom,validate_character_card,validate_conflict,validate_ruling,validate_nodes
try:
 from plotpilot_plugin_sdk.canonical import canonical_bytes as sdk_canonical_bytes
 from plotpilot_plugin_sdk.verifier import verify_checkpoint,verify_result_bundle,verify_provenance_receipt,validate_rpc_result
except ImportError: sdk_canonical_bytes=verify_checkpoint=verify_result_bundle=verify_provenance_receipt=validate_rpc_result=None
try:
 from jsonschema import Draft202012Validator
except ImportError: Draft202012Validator=None
try: from .package_identity import load_runtime_identity
except Exception: load_runtime_identity=None
ID_RE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$');HASH_RE=re.compile(r'^[0-9a-f]{64}$');TIME_RE=re.compile(r'^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.(?!000)[0-9]{3}Z)$')
CAPABILITY_ATOM_EXTRACT='analysis.character.atom.extract/v1';CAPABILITY_CARD_GENERATE='analysis.character.card.generate/v1';CAPABILITY_CONFLICT_REVIEW='analysis.character.conflict.review/v1';CAPABILITY_CONFLICT_APPLY='analysis.character.conflict.apply/v1'
class CharacterWorkerError(RuntimeError):
 def __init__(self,code,message,retryable=False):self.code=code;self.retryable=retryable;super().__init__(message)
class TerminalContractError(CharacterWorkerError):pass
class HostPort(Protocol):
 def call(self,method:str,params:Mapping[str,object])->Mapping[str,object]:...
@dataclass
class _Context:
 request_hash:str;binding_hash:str;last_event_seq:int=0;local_seq:int=0;stage_response:dict[str,Any]|None=None

def _id(v,label):
 if not isinstance(v,str) or ID_RE.fullmatch(v) is None:raise CharacterWorkerError('INPUT_INVALID',f'{label} must be an identifier')
 return v
def _hash(v,label):
 if not isinstance(v,str) or HASH_RE.fullmatch(v) is None:raise CharacterWorkerError('INPUT_INVALID',f'{label} must be lowercase SHA-256')
 return v
def _int(v,label,minv=0):
 if isinstance(v,bool) or not isinstance(v,int) or v<minv:raise CharacterWorkerError('INPUT_INVALID',f'{label} must be integer >= {minv}')
 return v
def _json_bytes(v):
 try:
  if sdk_canonical_bytes is not None:return bytes(sdk_canonical_bytes(v))
  return canonical_json_bytes(v)
 except Exception as exc:raise CharacterWorkerError('CANONICALIZATION_ERROR',str(exc)) from exc
def _strict_json(raw,code='ASSET_READ_ERROR'):
 def dup(pairs):
  out={}
  for k,v in pairs:
   if k in out:raise ValueError('duplicate key')
   out[k]=v
  return out
 try:
  v=json.loads(raw.decode('utf8','strict'),object_pairs_hook=dup,parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
 except Exception as exc:raise CharacterWorkerError(code,'Asset is not strict UTF-8 JSON') from exc
 return v
def _host_call(host,method,params):
 if host is None or not hasattr(host,'call'):raise CharacterWorkerError('HOST_REQUIRED','Core HostPort.call is required')
 try:r=host.call(method,dict(params))
 except CharacterWorkerError:raise
 except Exception as exc:raise CharacterWorkerError('HOST_RPC_ERROR',f'{method}: {exc}',True) from exc
 if not isinstance(r,Mapping):raise CharacterWorkerError('HOST_CONTRACT_ERROR',f'{method} returned non-object')
 if validate_rpc_result is not None:
  try:validate_rpc_result(method,dict(r),request={'method':method,'params':dict(params)})
  except Exception as exc:raise CharacterWorkerError('HOST_CONTRACT_ERROR',f'{method} result failed public RPC: {exc}') from exc
 return dict(r)
def _read_asset(host,asset_id,expected_hash,code='ASSET_READ_ERROR'):
 _id(asset_id,'asset_id');_hash(expected_hash,'asset_hash');
 if host is None:raise CharacterWorkerError('HOST_REQUIRED','asset reads require Core HostPort')
 offset=0;chunks=[]
 for _ in range(4096):
  r=_host_call(host,'host.asset.read/v1',{'asset_id':asset_id,'offset':offset,'length':8_388_608});enc=r.get('base64_chunk')
  if not isinstance(enc,str):raise CharacterWorkerError(code,'missing base64_chunk')
  try:chunk=base64.b64decode(enc,validate=True)
  except Exception as exc:raise CharacterWorkerError(code,'invalid base64') from exc
  if hashlib.sha256(chunk).hexdigest()!=r.get('content_hash'):raise CharacterWorkerError(code,'page hash mismatch')
  chunks.append(chunk);next_offset=r.get('next_offset')
  if next_offset is None:break
  if not isinstance(next_offset,int) or next_offset!=offset+len(chunk) or next_offset<=offset:raise CharacterWorkerError(code,'non-contiguous Asset pages')
  offset=next_offset
 else:raise CharacterWorkerError(code,'Asset exceeded page limit')
 data=b''.join(chunks)
 if hashlib.sha256(data).hexdigest()!=expected_hash:raise CharacterWorkerError(code,'Asset hash mismatch')
 return data
def _load_json_asset(host,asset_id,asset_hash,code='ASSET_READ_ERROR'):
 return _strict_json(_read_asset(host,asset_id,asset_hash,code),code)
def _upload(host,context,data,mime,suffix):
 digest=hashlib.sha256(data).hexdigest();asset_id=f'asset-{digest[:48]}'
 if host is None:return asset_id
 upload_id=context.request_hash+'-upload-'+suffix;offset=0;chunks=[data[i:i+8_388_608] for i in range(0,len(data),8_388_608)] or [b'']
 for i,chunk in enumerate(chunks):
  final=i==len(chunks)-1;r=_host_call(host,'host.asset.create/v1',{'operation_key':context.request_hash,'upload_id':upload_id,'offset':offset,'mime':mime,'total_size':len(data),'expected_hash':digest,'chunk_hash':hashlib.sha256(chunk).hexdigest(),'base64_chunk':base64.b64encode(chunk).decode('ascii'),'final':final})
  if r.get('upload_id')!=upload_id or r.get('accepted_bytes')!=offset+len(chunk) or r.get('completed') is not final:raise CharacterWorkerError('ASSET_CREATE_ERROR','upload acknowledgement is not contiguous')
  if final:
   if not isinstance(r.get('asset_id'),str):raise CharacterWorkerError('ASSET_CREATE_ERROR','final upload has no Asset ID')
   asset_id=r['asset_id']
  offset+=len(chunk)
 status=_host_call(host,'host.asset.upload.status/v1',{'upload_id':upload_id,'expected_hash':digest})
 if status.get('accepted_bytes')!=len(data) or status.get('completed') is not True or status.get('asset_id')!=asset_id:raise CharacterWorkerError('ASSET_UPLOAD_ERROR','upload status mismatch')
 return asset_id
def _validate_request(request,cap):
 if not isinstance(request,Mapping):raise CharacterWorkerError('INPUT_INVALID','request must be object')
 v=dict(request);spec=SPEC_BY_CAPABILITY[cap];common={'schema','capability_id','operation_key','operation','job_id','step_id','attempt_id','worker_run_id','lease_epoch','checkpoint_ids','provenance_receipt_id','created_at','total_units','run_snapshot_hash','workspace_id','document_id','source_revision_id'}
 groups={CAPABILITY_ATOM_EXTRACT:{'canonical_asset_id','canonical_text_hash','nodes','archetype_asset_id','archetype_asset_hash','model_profile_revision_id','character_id'},CAPABILITY_CARD_GENERATE:{'canonical_asset_id','canonical_text_hash','nodes','archetype_asset_id','archetype_asset_hash','model_profile_revision_id','character_id','atom_asset_id','atom_asset_hash'},CAPABILITY_CONFLICT_REVIEW:{'canonical_asset_id','canonical_text_hash','nodes','archetype_asset_id','archetype_asset_hash','model_profile_revision_id','conflict_asset_id','conflict_asset_hash'},CAPABILITY_CONFLICT_APPLY:{'canonical_asset_id','canonical_text_hash','nodes','archetype_asset_id','archetype_asset_hash','model_profile_revision_id','conflict_asset_id','conflict_asset_hash','ruling_asset_id','ruling_asset_hash'}}
 allowed=common|groups[cap];op=v.get('operation')
 if op not in spec.supports:raise CharacterWorkerError('INPUT_INVALID',f'operation {op!r} is not declared')
 if op=='resume':allowed|={'resume_checkpoint_asset_id','resume_checkpoint_asset_hash','resume_state_asset_id','resume_state_asset_hash'}
 if set(v)!=allowed:raise CharacterWorkerError('INPUT_INVALID',f'request fields are not closed: missing={sorted(allowed-set(v))}, extra={sorted(set(v)-allowed)}')
 if v['schema']!=spec.input_schema or v['capability_id']!=cap or v['operation_key']!=cap:raise CharacterWorkerError('INPUT_INVALID','request schema/capability/operation_key mismatch')
 for f in ('job_id','step_id','attempt_id','worker_run_id','provenance_receipt_id','workspace_id','document_id','source_revision_id'): _id(v[f],f)
 _int(v['lease_epoch'],'lease_epoch',1);_int(v['total_units'],'total_units',1);_hash(v['run_snapshot_hash'],'run_snapshot_hash')
 if not isinstance(v['created_at'],str) or TIME_RE.fullmatch(v['created_at']) is None:raise CharacterWorkerError('INPUT_INVALID','created_at has invalid UTC form')
 if not isinstance(v['checkpoint_ids'],list) or not v['checkpoint_ids'] or len(set(v['checkpoint_ids']))!=len(v['checkpoint_ids']):raise CharacterWorkerError('INPUT_INVALID','checkpoint_ids must be unique/non-empty')
 for f in ('canonical_asset_id','archetype_asset_id','atom_asset_id','conflict_asset_id','ruling_asset_id','model_profile_revision_id'):
  if f in v:_id(v[f],f)
 for f in ('canonical_text_hash','archetype_asset_hash','atom_asset_hash','conflict_asset_hash','ruling_asset_hash'):
  if f in v:_hash(v[f],f)
 if not isinstance(v['nodes'],list):raise CharacterWorkerError('INPUT_INVALID','nodes must be array')
 if op=='resume':
  for f in ('resume_checkpoint_asset_id','resume_state_asset_id'):_id(v[f],f)
  for f in ('resume_checkpoint_asset_hash','resume_state_asset_hash'):_hash(v[f],f)
 return v
def _context(v,cap):
 # ``operation`` and resume handles are transport controls.  Excluding them
 # keeps the binding stable from the original run to an exact resume while
 # retaining every source/job/plugin/snapshot field.
 binding={k:deepcopy(x) for k,x in v.items() if k!='operation' and not k.startswith('resume_')}
 return _Context(hash_json(cap+'-request/v1',v),hash_json(cap+'-binding/v1',binding))

def _event(v,context,host,event_type,payload_asset_id=None):
 if host is None:return
 context.local_seq+=1
 r=_host_call(host,'host.job.event/v1',{'operation_key':v['operation_key'],'event_type':event_type,'payload_asset_id':payload_asset_id,'local_seq':context.local_seq})
 if r.get('accepted') is not True:raise CharacterWorkerError('HOST_REJECTED',f'Core rejected job event {event_type}')
def _source(v,host):
 raw=_read_asset(host,v['canonical_asset_id'],v['canonical_text_hash'])
 try:text=raw.decode('utf8','strict')
 except UnicodeDecodeError as exc:raise CharacterWorkerError('ASSET_READ_ERROR','canonical Asset is not UTF-8') from exc
 if sha256_text(text)!=v['canonical_text_hash']:raise CharacterWorkerError('ASSET_READ_ERROR','canonical Revision hash mismatch')
 try:nodes=validate_nodes(v['nodes'],len(text))
 except CharacterContractError as exc:raise CharacterWorkerError('EVIDENCE_INVALID',str(exc)) from exc
 return text,nodes
def _prov(v,cap,package_hash,release_id,model_receipt_id=None):return {'plugin_id':PLUGIN_ID,'capability_id':cap,'package_hash':package_hash,'release_id':release_id,'run_snapshot_hash':v['run_snapshot_hash'],'model_receipt_id':model_receipt_id}
def _candidate_item(v,payload,payload_asset_id,payload_schema,item_id,workspace_id,revision_id):
 payload_hash=hashlib.sha256(payload).hexdigest()
 return {'schema':'candidate-item/v1','item_id':item_id,'item_kind':'relation_set','target':{'workspace_id':workspace_id,'entity_kind':'relation_set','entity_id':item_id},'mutation':{'mode':'relation_patch','payload_schema':payload_schema,'payload_hash':payload_hash},'payload_asset_id':payload_asset_id,'base':{'revision_id':revision_id,'content_hash':v['canonical_text_hash']},'write_set':[{'workspace_id':workspace_id,'entity_kind':'relation_set','entity_id':item_id,'revision_id':revision_id,'content_hash':v['canonical_text_hash']}],'parent_candidate_ids':[],'source_refs':[{'workspace_id':workspace_id,'source_type':'revision','source_id':v['document_id'],'revision_or_hash':revision_id}], 'status':'complete'}
def _bundle(v,cap,items,release_id,contract_id='candidate-batch/v1',partial=False,warnings=None):
 return {'schema':'result-bundle/v1','contract_id':contract_id,'bundle_id':f'{cap.replace("/","-")}-{v["attempt_id"]}-bundle','bundle_type':'candidate_batch' if contract_id=='candidate-batch/v1' else 'diagnostic','producer':{'plugin_id':PLUGIN_ID,'release_id':release_id,'capability_id':cap,'job_id':v['job_id'],'step_id':v['step_id'],'attempt_id':v['attempt_id'],'lease_epoch':v['lease_epoch']},'input_snapshot_hash':v['run_snapshot_hash'],'items':items,'warnings':warnings or [],'partial':partial,'provenance_receipt_id':v['provenance_receipt_id'],'skill_chain_result_refs':[]}
def _diagnostic(v,cap,release_id,code,message,source=True):
 refs=[{'workspace_id':v['workspace_id'],'source_type':'revision','source_id':v['document_id'],'revision_or_hash':v['source_revision_id']}] if source else []
 item={'schema':'diagnostic-item/v1','item_id':f'{cap.replace("/","-")}-{v["attempt_id"]}-diagnostic','severity':'error','code':code,'message':message,'details_asset_id':None,'details_hash':None,'source_refs':refs,'status':'complete'}
 return _bundle(v,cap,[item],release_id,'diagnostic-bundle/v1')

def _verify_bundle_payloads(bundle,host,expected_contract):
 if bundle.get('contract_id')!=expected_contract:raise CharacterWorkerError('RESULT_INVALID','result contract does not match capability')
 if host is None:raise CharacterWorkerError('HOST_REQUIRED','result payload verification requires HostPort')
 for item in bundle.get('items',[]):
  if item.get('schema')=='candidate-item/v1':
   expected=item['mutation']['payload_hash'];schema=item['mutation']['payload_schema']
  elif item.get('schema')=='artifact-item/v1':
   expected=item['payload_hash'];schema=None
  else:continue
  raw=_read_asset(host,item['payload_asset_id'],expected)
  if schema is not None:
   try:payload=_strict_json(raw,'RESULT_INVALID')
   except Exception as exc:raise CharacterWorkerError('RESULT_INVALID',f'Candidate payload JSON invalid: {exc}') from exc
   if not isinstance(payload,Mapping) or payload.get('schema')!=schema:raise CharacterWorkerError('RESULT_INVALID','Candidate payload schema drift')
   # Result Bundle verification only covers the public Candidate envelope;
   # the payload itself is a plugin-owned closed schema and must be checked
   # before staging and again on resume.  Loading failure is terminal rather
   # than a permissive fallback.
   if schema in {'character-atom/v1','character-card/v1'}:
    if Draft202012Validator is None:raise CharacterWorkerError('RESULT_INVALID','payload schema verifier unavailable')
    path=__import__('pathlib').Path(__file__).resolve().parent/'schemas'/('character-atom-v1.schema.json' if schema=='character-atom/v1' else 'character-card-v1.schema.json')
    try: schema_value=json.loads(path.read_text(encoding='utf8')); errors=sorted(Draft202012Validator(schema_value).iter_errors(dict(payload)),key=lambda e:list(e.absolute_path))
    except Exception as exc:raise CharacterWorkerError('RESULT_INVALID','payload schema unavailable') from exc
    if errors:raise CharacterWorkerError('RESULT_INVALID',f'payload {schema} schema: {errors[0].message}')
   elif schema=='character-conflict-resolution/v1':
    if set(payload)!={'schema','conflict_id','field','selected_sources','ruling','authority'} or payload.get('authority')!='candidate_only' or not isinstance(payload.get('selected_sources'),list) or not payload['selected_sources']:
     raise CharacterWorkerError('RESULT_INVALID','conflict resolution payload is not closed')
class CharacterDistillationPlugin:
 def __init__(self):
  self.package_hash,self.release_id=self._identity();self.last_receipt=None;self.last_stage_response=None;self.last_checkpoint=None;self.last_state=None;self.last_model_receipt_ids=[]
 def _identity(self):
  try:
   x=load_runtime_identity() if load_runtime_identity else None
   if x:return str(x['package_hash']),str(x['release_id'])
  except Exception as exc:raise CharacterWorkerError('PACKAGE_IDENTITY_ERROR',str(exc)) from exc
  raise CharacterWorkerError('PACKAGE_IDENTITY_ERROR','package identity sidecar is unavailable')
 def _stage(self,v,context,host,bundle,bundle_asset_id):
  if bundle['contract_id']!='candidate-batch/v1':return None
  if host is None:return None
  r=_host_call(host,'host.candidate.stage/v1',{'operation_key':v['operation_key'],'result_bundle_asset_id':bundle_asset_id,'input_snapshot_hash':v['run_snapshot_hash']})
  rows=r.get('staged_items');expected=[item['item_id'] for item in bundle['items']]
  if r.get('accepted') is not True or not isinstance(rows,list):raise CharacterWorkerError('HOST_REJECTED','Core rejected Candidate staging')
  if [row.get('item_id') for row in rows]!=expected or any(not isinstance(row.get('candidate_id'),str) or row.get('stage_status')!='created' for row in rows):raise CharacterWorkerError('HOST_CONTRACT_ERROR','stage response does not bind Candidate items')
  context.stage_response=r;self.last_stage_response=deepcopy(r);return r
 def _persist_checkpoint(self,v,context,host,bundle_asset_id,bundle_hash):
  """Persist a checkpoint/state pair whose every identity component is exact."""
  state_unsigned={"schema":"character-resume-state/v1","state_id":v["attempt_id"]+"-state","plugin_id":PLUGIN_ID,"capability_id":v["capability_id"],"package_hash":self.package_hash,"release_id":self.release_id,"job_id":v["job_id"],"step_id":v["step_id"],"attempt_id":v["attempt_id"],"lease_epoch":v["lease_epoch"],"checkpoint_id":v["checkpoint_ids"][0],"checkpoint_seq":1,"run_snapshot_hash":v["run_snapshot_hash"],"workspace_id":v["workspace_id"],"binding_hash":context.binding_hash,"result_bundle_asset_id":bundle_asset_id,"result_bundle_hash":bundle_hash}
  state={**state_unsigned,"state_hash":hash_json("character-resume-state/v1",state_unsigned)}
  state_bytes=_json_bytes(state);state_asset_id=_upload(host,context,state_bytes,"application/json","state")
  checkpoint_unsigned={"schema":"checkpoint/v1","checkpoint_id":v["checkpoint_ids"][0],"checkpoint_seq":1,"job_id":v["job_id"],"step_id":v["step_id"],"source_attempt_id":v["attempt_id"],"lease_epoch":v["lease_epoch"],"run_snapshot_hash":v["run_snapshot_hash"],"replay_policy":"checkpoint_resume","completed_units":1,"total_units":v["total_units"],"unit_set_hash":state["state_hash"],"state_asset_id":state_asset_id,"created_at":v["created_at"]}
  checkpoint={**checkpoint_unsigned,"checkpoint_hash":hash_json("checkpoint/v1",checkpoint_unsigned)}
  cp_bytes=_json_bytes(checkpoint);cp_asset_id=_upload(host,context,cp_bytes,"application/json","checkpoint")
  if host is not None:
   response=_host_call(host,"host.checkpoint.commit/v1",{"operation_key":v["operation_key"],"checkpoint_asset_id":cp_asset_id})
   if response.get("accepted") is not True or response.get("checkpoint_id")!=checkpoint["checkpoint_id"]:raise CharacterWorkerError("CHECKPOINT_INVALID","Core rejected or changed checkpoint identity")
  self.last_state=deepcopy(state);self.last_checkpoint=deepcopy(checkpoint)
  return checkpoint,state
 def _complete(self,v,context,host,outcome,bundle_asset_id,stage_key=None,detail_asset_id=None):
  if host is None:return
  context.local_seq+=1
  r=_host_call(host,'host.job.complete/v1',{'operation_key':v['operation_key'],'worker_run_id':v['worker_run_id'],'outcome':outcome,'result_bundle_asset_id':bundle_asset_id,'candidate_stage_operation_key':stage_key,'terminal_detail_asset_id':detail_asset_id,'local_seq':context.local_seq})
  if r.get('accepted') is not True or r.get('attempt_state')!=outcome or r.get('step_state')!=outcome or r.get('job_state')!=outcome or r.get('provenance_receipt_id')!=v['provenance_receipt_id']:raise CharacterWorkerError('HOST_CONTRACT_ERROR','job completion acknowledgement drift')
 def _make_outputs(self,v,cap,host,context):
  text,nodes=_source(v,host);archetype=validate_archetype(_load_json_asset(host,v['archetype_asset_id'],v['archetype_asset_hash']))
  span=build_evidence_span(workspace_id=v['workspace_id'],document_id=v['document_id'],revision_id=v['source_revision_id'],node_id=nodes[0]['node_id'],start_codepoint=nodes[0]['start_codepoint'],end_codepoint=nodes[0]['end_codepoint'],canonical_text=text,node_range=nodes[0])
  prov=_prov(v,cap,self.package_hash,self.release_id)
  if cap==CAPABILITY_ATOM_EXTRACT:
   dim=archetype['dimensions'][0]['dimension_id'];kind=archetype['atom_kinds'][0];payload={'schema':'character-atom/v1','atom_id':f'character-atom-{v["character_id"] if "character_id" in v else v["document_id"]}','character_id':v.get('character_id',f'character-{v["document_id"]}'),'dimension':dim,'kind':kind,'observation':text[:120] or 'observation','interpretation':'Derived from exact source evidence','confidence':'confirmed','source_revision_id':v['source_revision_id'],'evidence_spans':[span],'provenance':prov,'authority':'candidate_only'}
   payload=validate_character_atom(payload,archetype=archetype,canonical_text=text,nodes=nodes,workspace_id=v['workspace_id'],document_id=v['document_id'],revision_id=v['source_revision_id'],canonical_text_hash=v['canonical_text_hash'])
   schema='character-atom/v1'; item_id=payload['atom_id']
  elif cap==CAPABILITY_CARD_GENERATE:
   atom_payload=_load_json_asset(host,v['atom_asset_id'],v['atom_asset_hash']);
   if isinstance(atom_payload,dict) and atom_payload.get('schema')=='character-atom/v1':
    atom=validate_character_atom(atom_payload,archetype=archetype,canonical_text=text,nodes=nodes,workspace_id=v['workspace_id'],document_id=v['document_id'],revision_id=v['source_revision_id'],canonical_text_hash=v['canonical_text_hash'])
    if atom.get('character_id')!=v['character_id']:
     raise CharacterWorkerError('INPUT_INVALID','atom character identity does not match card request')
    atom_provenance=atom.get('provenance',{})
    if atom_provenance.get('plugin_id')!=PLUGIN_ID or atom_provenance.get('capability_id')!=CAPABILITY_ATOM_EXTRACT or atom_provenance.get('package_hash')!=self.package_hash or atom_provenance.get('release_id')!=self.release_id or atom_provenance.get('run_snapshot_hash')!=v['run_snapshot_hash']:
     raise CharacterWorkerError('INPUT_INVALID','atom provenance is foreign, stale, or outside the current RunSnapshot')
   else:raise CharacterWorkerError('INPUT_INVALID','atom Asset is not a closed character-atom/v1')
   cid=v['character_id'];payload={'schema':'character-card/v1','character_id':cid,'name':cid,'identity':atom['observation'],'goals':['Pursue the declared objective'],'motivations':['Protect a meaningful stake'],'fears':['Failure'],'abilities':['Persistence'],'flaws':['Uncertainty'],'relationships':[],'conflicts':[atom['interpretation']],'arc':{'stage':archetype['arc_stages'][0],'stages':[archetype['arc_stages'][0]],'summary':atom['interpretation'],'key_events':[atom['observation']]},'speech_habits':['Measured speech'],'behavior_evidence':[span],'source_revision_id':v['source_revision_id'],'source_refs':[{'workspace_id':v['workspace_id'],'document_id':v['document_id'],'revision_id':v['source_revision_id']}],'authority':'candidate_only','provenance':prov}
   payload=validate_character_card(payload,archetype=archetype,canonical_text=text,nodes=nodes,workspace_id=v['workspace_id'],document_id=v['document_id'],revision_id=v['source_revision_id'],canonical_text_hash=v['canonical_text_hash']);schema='character-card/v1';item_id=f'character-card-{cid}'
  elif cap==CAPABILITY_CONFLICT_REVIEW:
   conflict=validate_conflict(_load_json_asset(host,v['conflict_asset_id'],v['conflict_asset_hash']),canonical_text=text,nodes=nodes,workspace_id=v['workspace_id'],document_id=v['document_id'],revision_id=v['source_revision_id'],canonical_text_hash=v['canonical_text_hash']);return _bundle(v,cap,[{'schema':'diagnostic-item/v1','item_id':conflict['conflict_id']+'-diagnostic','severity':'warning','code':'CHARACTER_CONFLICT','message':'Conflicting character values require an explicit user ruling','details_asset_id':None,'details_hash':None,'source_refs':[{'workspace_id':v['workspace_id'],'source_type':'revision','source_id':v['document_id'],'revision_or_hash':v['source_revision_id']}],'status':'complete'}],self.release_id,'diagnostic-bundle/v1')
  else:
   conflict=validate_conflict(_load_json_asset(host,v['conflict_asset_id'],v['conflict_asset_hash']),canonical_text=text,nodes=nodes,workspace_id=v['workspace_id'],document_id=v['document_id'],revision_id=v['source_revision_id'],canonical_text_hash=v['canonical_text_hash']);ruling=validate_ruling(_load_json_asset(host,v['ruling_asset_id'],v['ruling_asset_hash']),conflict);payload=apply_conflict_ruling(conflict,ruling);payload['schema']='character-conflict-resolution/v1';schema='character-conflict-resolution/v1';item_id=conflict['conflict_id']+'-resolution'
  data=_json_bytes(payload);aid=_upload(host,context,data,'application/json',item_id);item=_candidate_item(v,data,aid,schema,item_id,v['workspace_id'],v['source_revision_id']);return _bundle(v,cap,[item],self.release_id)
 def _resume(self,v,cap,context,host):
  checkpoint_raw=_read_asset(host,v['resume_checkpoint_asset_id'],v['resume_checkpoint_asset_hash'])
  checkpoint=_strict_json(checkpoint_raw,'RESUME_INVALID')
  if checkpoint_raw!=_json_bytes(checkpoint):raise CharacterWorkerError('RESUME_INVALID','checkpoint is not canonical JSON')
  if verify_checkpoint is not None:
   try:verify_checkpoint(checkpoint,expected_snapshot_hash=v['run_snapshot_hash'])
   except Exception as exc:raise CharacterWorkerError('RESUME_INVALID',f'checkpoint verification failed: {exc}') from exc
  if checkpoint.get('checkpoint_id') not in v['checkpoint_ids'] or checkpoint.get('state_asset_id')!=v['resume_state_asset_id'] or checkpoint.get('total_units')!=v['total_units'] or checkpoint.get('completed_units')!=v['total_units'] or checkpoint.get('job_id')!=v['job_id'] or checkpoint.get('step_id')!=v['step_id'] or checkpoint.get('source_attempt_id')!=v['attempt_id'] or checkpoint.get('lease_epoch')!=v['lease_epoch'] or checkpoint.get('run_snapshot_hash')!=v['run_snapshot_hash']:raise CharacterWorkerError('RESUME_INVALID','checkpoint identity does not exactly bind request/state')
  state_raw=_read_asset(host,v['resume_state_asset_id'],v['resume_state_asset_hash'])
  state=_strict_json(state_raw,'RESUME_INVALID')
  if state_raw!=_json_bytes(state):raise CharacterWorkerError('RESUME_INVALID','resume state is not canonical JSON')
  expected_state={'schema':'character-resume-state/v1','state_id':state.get('state_id'),'plugin_id':PLUGIN_ID,'capability_id':cap,'package_hash':self.package_hash,'release_id':self.release_id,'job_id':v['job_id'],'step_id':v['step_id'],'attempt_id':v['attempt_id'],'lease_epoch':v['lease_epoch'],'checkpoint_id':checkpoint['checkpoint_id'],'checkpoint_seq':checkpoint['checkpoint_seq'],'run_snapshot_hash':v['run_snapshot_hash'],'workspace_id':v['workspace_id'],'binding_hash':context.binding_hash,'result_bundle_asset_id':state.get('result_bundle_asset_id'),'result_bundle_hash':state.get('result_bundle_hash'),'state_hash':state.get('state_hash')}
  if not isinstance(state,dict) or set(state)!=set(expected_state) or state.get('state_id')!=v['attempt_id']+'-state' or any(state.get(k)!=val for k,val in expected_state.items() if k not in {'state_id','state_hash'}):raise CharacterWorkerError('RESUME_INVALID','resume state plugin/capability/package/release/binding drift')
  if checkpoint.get('unit_set_hash')!=state.get('state_hash'):raise CharacterWorkerError('RESUME_INVALID','checkpoint unit_set_hash does not bind state')
  unsigned={k:x for k,x in state.items() if k!='state_hash'}
  if hashlib.sha256(state_raw).hexdigest()!=v['resume_state_asset_hash']:raise CharacterWorkerError('RESUME_INVALID','resume state Asset hash mismatch')
  if state.get('state_hash')!=hash_json('character-resume-state/v1',unsigned):raise CharacterWorkerError('RESUME_INVALID','resume state hash mismatch')
  raw=_read_asset(host,state['result_bundle_asset_id'],state['result_bundle_hash'],'RESUME_INVALID');bundle=_strict_json(raw,'RESUME_INVALID')
  if raw!=_json_bytes(bundle):raise CharacterWorkerError('RESUME_INVALID','resume Bundle must be canonical JSON')
  if verify_result_bundle is not None:
   try:verify_result_bundle(bundle,snapshot_workspace_id=v['workspace_id'] if bundle.get('contract_id')=='candidate-batch/v1' else None,snapshot_hash_value=v['run_snapshot_hash'])
   except Exception as exc:raise CharacterWorkerError('RESUME_INVALID',f'result Bundle verification failed: {exc}') from exc
  if bundle.get('producer')!={'plugin_id':PLUGIN_ID,'release_id':self.release_id,'capability_id':cap,'job_id':v['job_id'],'step_id':v['step_id'],'attempt_id':v['attempt_id'],'lease_epoch':v['lease_epoch']} or bundle.get('contract_id')!=SPEC_BY_CAPABILITY[cap].result_contract:raise CharacterWorkerError('RESUME_INVALID','result producer/contract drift')
  _verify_bundle_payloads(bundle,host,SPEC_BY_CAPABILITY[cap].result_contract)
  self.last_state=deepcopy(state);self.last_checkpoint=deepcopy(checkpoint);stage_key=None
  if bundle.get('contract_id')=='candidate-batch/v1':
   stage_asset=state['result_bundle_asset_id'];self._stage(v,context,host,bundle,stage_asset);stage_key=v['operation_key']
  self._complete(v,context,host,'succeeded',state['result_bundle_asset_id'],stage_key)
  receipt_unsigned={'schema':'provenance-receipt/v1','receipt_id':v['provenance_receipt_id'],'plugin_id':PLUGIN_ID,'release_id':self.release_id,'package_hash':self.package_hash,'capability_id':cap,'job_id':v['job_id'],'step_id':v['step_id'],'attempt_id':v['attempt_id'],'lease_epoch':v['lease_epoch'],'run_snapshot_hash':v['run_snapshot_hash'],'bundle_id':bundle['bundle_id'],'bundle_hash':state['result_bundle_hash'],'parent_receipt_ids':[],'model_receipt_ids':[],'skill_chain_result_refs':[],'staged_items':[i['item_id'] for i in bundle['items']] if stage_key else [],'created_at':v['created_at']}
  self.last_receipt={**receipt_unsigned,'receipt_hash':hash_json('provenance-receipt/v1',receipt_unsigned)};return bundle
 def _failure(self,v,cap,context,host,error):
  code=getattr(error,'code','INTERNAL_ERROR');bundle=_diagnostic(v,cap,self.release_id,code,str(error),source=isinstance(v,Mapping) and 'workspace_id' in v)
  try:
   if context is not None:
    data=_json_bytes(bundle);aid=_upload(host,context,data,'application/json','failure');self._complete(v,context,host,'failed',aid,None,None)
  except Exception:pass
  self.last_stage_response=None;self.last_receipt=None;return bundle
 def run(self,request,host=None):
  cap=None;v=None;context=None
  try:
   if not isinstance(request,Mapping):raise CharacterWorkerError('INPUT_INVALID','request must be object')
   v=dict(request);cap=v.get('capability_id') or next((s.capability_id for s in SPEC_BY_CAPABILITY.values() if s.input_schema==v.get('schema')),None)
   if cap not in CAPABILITIES:raise CharacterWorkerError('CAPABILITY_UNKNOWN','unknown character capability')
   v=_validate_request(v,cap);context=_context(v,cap)
   if v['operation']=='cancel':
    self._complete(v,context,host,'cancelled',None,None);return None
   if v['operation']=='resume':return self._resume(v,cap,context,host)
   _event(v,context,host,'character-run-start')
   bundle=self._make_outputs(v,cap,host,context)
   _verify_bundle_payloads(bundle,host,SPEC_BY_CAPABILITY[cap].result_contract)
   if v['operation']=='validate':return bundle
   raw=_json_bytes(bundle);bundle_asset_id=_upload(host,context,raw,'application/json','result');
   if verify_result_bundle is not None:
    try:verify_result_bundle(bundle,snapshot_workspace_id=v['workspace_id'] if bundle.get('contract_id')=='candidate-batch/v1' else None,snapshot_hash_value=v['run_snapshot_hash'])
    except Exception as exc:raise CharacterWorkerError('RESULT_INVALID',str(exc)) from exc
   _event(v,context,host,'result-ready',bundle_asset_id)
   self._persist_checkpoint(v,context,host,bundle_asset_id,hashlib.sha256(raw).hexdigest());stage=self._stage(v,context,host,bundle,bundle_asset_id);self._complete(v,context,host,'succeeded',bundle_asset_id,v['operation_key'] if stage else None)
   receipt_unsigned={'schema':'provenance-receipt/v1','receipt_id':v['provenance_receipt_id'],'plugin_id':PLUGIN_ID,'release_id':self.release_id,'package_hash':self.package_hash,'capability_id':cap,'job_id':v['job_id'],'step_id':v['step_id'],'attempt_id':v['attempt_id'],'lease_epoch':v['lease_epoch'],'run_snapshot_hash':v['run_snapshot_hash'],'bundle_id':bundle['bundle_id'],'bundle_hash':hashlib.sha256(raw).hexdigest(),'parent_receipt_ids':[],'model_receipt_ids':[],'skill_chain_result_refs':[],'staged_items':[i['item_id'] for i in bundle['items']] if bundle['contract_id']=='candidate-batch/v1' else [],'created_at':v['created_at']}
   self.last_receipt={**receipt_unsigned,'receipt_hash':hash_json('provenance-receipt/v1',receipt_unsigned)}
   return bundle
  except TerminalContractError:raise
  except CharacterWorkerError as exc:
   # A closed-request rejection happens before the normal context is built.
   # When the transport still carries the durable attempt identity, create a
   # best-effort context so Core receives the failed diagnostic Result and can
   # close the attempt; malformed/incomplete requests remain side-effect free.
   if context is None and cap in CAPABILITIES and isinstance(v,Mapping):
    try:
     required={'operation_key','worker_run_id','provenance_receipt_id','attempt_id','job_id','step_id','lease_epoch','run_snapshot_hash','workspace_id'}
     if required <= set(v): context=_context(v,cap)
    except Exception: context=None
   return self._failure(v or request,cap,context,host,exc) if cap in CAPABILITIES else None
  except (CharacterContractError,Exception) as exc:
   err=CharacterWorkerError('INPUT_INVALID' if isinstance(exc,CharacterContractError) else 'INTERNAL_ERROR',str(exc))
   if context is None and cap in CAPABILITIES and isinstance(v,Mapping):
    try:
     required={'operation_key','worker_run_id','provenance_receipt_id','attempt_id','job_id','step_id','lease_epoch','run_snapshot_hash','workspace_id'}
     if required <= set(v): context=_context(v,cap)
    except Exception: context=None
   return self._failure(v or request,cap,context,host,err) if cap in CAPABILITIES else None

def capability_descriptor(capability_id=None):
 cap=capability_id or CAPABILITY_ATOM_EXTRACT
 package_hash,release_id=CharacterDistillationPlugin().package_hash,CharacterDistillationPlugin().release_id
 return SPEC_BY_CAPABILITY[cap].descriptor(release_id)
PACKAGE_HASH,RELEASE_ID=CharacterDistillationPlugin()._identity();DESCRIPTORS=descriptors(RELEASE_ID);_RUNTIME=CharacterDistillationPlugin()
def main(request=None,host=None):return capability_descriptor() if request is None else _RUNTIME.run(request,host)
__all__=['CAPABILITIES','CAPABILITY_ATOM_EXTRACT','CAPABILITY_CARD_GENERATE','CAPABILITY_CONFLICT_REVIEW','CAPABILITY_CONFLICT_APPLY','CharacterDistillationPlugin','CharacterWorkerError','TerminalContractError','HostPort','PACKAGE_HASH','RELEASE_ID','DESCRIPTORS','capability_descriptor','main']
