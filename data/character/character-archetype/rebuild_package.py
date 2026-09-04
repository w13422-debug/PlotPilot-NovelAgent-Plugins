"""Deterministic builder for the character-archetype/v1 Data package."""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent/'v1'
PLUGIN_ID='com.plotpilot.novelagent.character-archetype'; FORMAT_ID='character-archetype/v1'; VERSION='1.0.0'
PAYLOAD=('data/archetypes.json','fixtures/interpreter-mappings.json','fixtures/interpreter-roundtrip.json','plugin.json','schemas/character-archetype-v1.schema.json','schemas/interpreter-roundtrip-v1.schema.json')
CAPABILITIES=("analysis.character.atom.extract/v1","analysis.character.card.generate/v1","analysis.character.conflict.review/v1","analysis.character.conflict.apply/v1","asset.template.derive/v1","writing.context.assemble/v1","writing.chapter.draft/v1")
PLUGINS=("com.plotpilot.novelagent.character-distillation",)*4+("com.plotpilot.novelagent.asset-derivation","com.plotpilot.novelagent.writing-context","com.plotpilot.novelagent.chapter-workflow")
def _sha(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def _json(v:object)->bytes:return (json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode('utf8')
def _canonical(v:object)->bytes:
 try:
  from plotpilot_plugin_sdk.canonical import canonical_bytes
  return bytes(canonical_bytes(v))
 except Exception:
  return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')
def _load(rel):
 v=json.loads((ROOT/rel).read_text(encoding='utf8'))
 if not isinstance(v,dict): raise ValueError(rel+' must be object')
 return v
def _exact(v,keys,label):
 if set(v)!=set(keys): raise ValueError(f'{label} fields differ: expected={sorted(keys)} actual={sorted(v)}')
def _validate():
 value=_load('data/archetypes.json')
 required={'schema','format_id','archetype_id','version','dimensions','atom_kinds','card_fields','relation_kinds','arc_stages','interpreter_mappings'}; _exact(value,required,'archetype')
 if value['schema']!=FORMAT_ID or value['format_id']!=FORMAT_ID or value['version']!=VERSION: raise ValueError('archetype identity drift')
 if not isinstance(value['dimensions'],list) or not value['dimensions']: raise ValueError('dimensions required')
 if not all(isinstance(item,dict) and set(item)=={'dimension_id','label','description','required','allowed_values'} for item in value['dimensions']): raise ValueError('dimensions schema drift')
 expected={(plugin,cap,direction) for plugin,cap in zip(PLUGINS,CAPABILITIES) for direction in ('format_to_capability','capability_to_format')}
 actual={(item.get('plugin_id'),item.get('capability_id'),item.get('direction')) for item in value['interpreter_mappings']}
 if actual!=expected or len(value['interpreter_mappings'])!=14: raise ValueError('interpreter mapping is not exact 14-entry bidirectional set')
 rt=_load('fixtures/interpreter-roundtrip.json'); _exact(rt,{'schema','format_id','capabilities','forward_direction','reverse_direction'},'roundtrip')
 if tuple(rt['capabilities'])!=CAPABILITIES: raise ValueError('roundtrip capability order drift')
 mapping=_load('fixtures/interpreter-mappings.json'); _exact(mapping,{'schema','format_id','mappings'},'mapping fixture')
 if mapping['mappings']!=value['interpreter_mappings']: raise ValueError('mapping fixture differs from data')
 try:
  from jsonschema import Draft202012Validator
  schema=_load('schemas/character-archetype-v1.schema.json'); Draft202012Validator.check_schema(schema); Draft202012Validator(schema).validate(value)
 except ImportError: pass
def _generated():
 # SDK is authoritative for package identity; add repository sdk only when run from checkout.
 sdk=next((parent/'sdk' for parent in ROOT.parents if (parent/'sdk').exists()), ROOT.parents[3]/'sdk')
 if sdk.exists() and str(sdk) not in sys.path: sys.path.insert(0,str(sdk))
 _validate(); files={p:(ROOT/p).read_bytes() for p in PAYLOAD}
 try:
  from plotpilot_plugin_sdk.package import digest_package
  digest=digest_package(files,PLUGIN_ID,VERSION); package_hash,release_id=digest.package_hash,digest.release_id; manifest=digest.files_sha256
 except Exception:
  manifest=''.join(f'{_sha(files[p])}  {p}\n' for p in sorted(files,key=lambda x:x.encode())).encode(); package_hash=_sha(b'plotpilot-package/v1\n'+manifest); release_id=_sha(f'plotpilot-release/v1\n{PLUGIN_ID}\n{VERSION}\n{package_hash}\n'.encode('ascii'))
 root_bytes=(ROOT/'data/archetypes.json').read_bytes()
 base={'schema':'plugin-data-bundle/v1','bundle_id':'character-archetype-v1-bundle','data_plugin_id':PLUGIN_ID,'data_release_id':release_id,'package_hash':package_hash,'format_id':FORMAT_ID,'root_path':'data/archetypes.json','files':[{'path':'data/archetypes.json','asset_id':'asset-character-archetype-v1','sha256':_sha(root_bytes),'mime':'application/json','size':len(root_bytes)}]}
 bundle={**base,'bundle_hash':_sha(b'plugin-data-bundle/v1\n'+_canonical(base))}
 identity={'schema':'character-archetype-package-identity/v1','package_kind':'data','plugin_id':PLUGIN_ID,'format_id':FORMAT_ID,'version':VERSION,'package_hash':package_hash,'release_id':release_id,'separate_from_code_plugin_id':'com.plotpilot.novelagent.character-distillation'}
 expected={'schema':'character-archetype-expected/v1','package_kind':'data','plugin_id':PLUGIN_ID,'format_id':FORMAT_ID,'version':VERSION,'manifest_path':'plugin.json','root_path':'data/archetypes.json','package_files':list(PAYLOAD),'files_sha256':manifest.decode('utf8'),'fixture_hashes':{p:_sha((ROOT/p).read_bytes()) for p in PAYLOAD if p.startswith(('fixtures/','schemas/'))},'package_hash':package_hash,'release_id':release_id,'bundle_hash':bundle['bundle_hash'],'identity_path':'identity.json','identity_domains':{'data_or_code':'plotpilot-release/v1','skill':'plotpilot-skill-release/v1','code_plugin_id':'com.plotpilot.novelagent.character-distillation'}}
 return {'files.sha256':manifest,'identity.json':_json(identity),'expected.json':_json(expected),'fixtures/plugin-data-bundle.json':_json(bundle)}
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--rebuild',action='store_true'); args=ap.parse_args(); generated=_generated()
 if args.rebuild:
  for n,b in generated.items(): (ROOT/n).write_bytes(b)
 for n,b in generated.items():
  if not (ROOT/n).is_file() or (ROOT/n).read_bytes()!=b: raise SystemExit('DRIFT '+n)
 e=json.loads(generated['expected.json']); print(json.dumps({'format_id':FORMAT_ID,'package_hash':e['package_hash'],'release_id':e['release_id'],'status':'PASS'},sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
