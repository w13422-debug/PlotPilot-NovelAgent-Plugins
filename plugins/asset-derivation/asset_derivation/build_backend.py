"""Deterministic wheel and outer package builder for Asset Derivation."""
from __future__ import annotations
import base64,csv,hashlib,io,json,sys,zipfile
from pathlib import Path
from .capability_spec import CAPABILITY_SPECS,DESCRIPTOR_PATHS,INDEX_BY_CAPABILITY,PLUGIN_ID,VERSION,capability_projection,descriptor_files,plugin_manifest,ui_metadata
NAME='plotpilot_asset_derivation';MODULE='asset_derivation';DIST_INFO=f'{NAME}-{VERSION}.dist-info';WHEEL_NAME=f'{NAME}-{VERSION}-py3-none-any.whl'
def _entry(name):
 i=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));i.compress_type=zipfile.ZIP_DEFLATED;i.create_system=3;i.external_attr=0o644<<16;return i
def _write_json(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
def _metadata():return {f'{DIST_INFO}/METADATA':f'Metadata-Version: 2.3\nName: plotpilot-asset-derivation\nVersion: {VERSION}\nSummary: Exact Asset Derivation Code Plugin\nRequires-Python: >=3.12,<3.13\n\n'.encode(),f'{DIST_INFO}/WHEEL':b'Wheel-Version: 1.0\nGenerator: nap04-deterministic-wheel-builder/1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',f'{DIST_INFO}/top_level.txt':b'asset_derivation\n'}
def build_canonical_wheel(package_root:Path,wheel_directory:Path)->Path:
 files={}
 for path in sorted((package_root/MODULE).rglob('*'),key=lambda p:p.relative_to(package_root).as_posix().encode()):
  # Cache directories are generated state, not package content.  Excluding
  # them here keeps wheel bytes and the outer package identity stable even
  # when tests or linters have run in the plugin tree.
  if path.is_file() and path.suffix in {'.py','.json'} and path.name!='identity.json' and not any(part in {'__pycache__','.pytest_cache','.ruff_cache','.mypy_cache'} for part in path.parts):files[path.relative_to(package_root).as_posix()]=path.read_bytes()
 files.update(_metadata());rows=[]
 for name in sorted(files,key=lambda x:x.encode()):rows.append([name,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).decode().rstrip('='),str(len(files[name]))])
 rows.append([f'{DIST_INFO}/RECORD','','']);out=io.StringIO(newline='');csv.writer(out,lineterminator='\n').writerows(rows);files[f'{DIST_INFO}/RECORD']=out.getvalue().encode()
 wheel_directory.mkdir(parents=True,exist_ok=True);target=wheel_directory/WHEEL_NAME
 with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
  for name in sorted(files,key=lambda x:x.encode()):z.writestr(_entry(name),files[name])
 return target
def _payload(root):
 excluded={'files.sha256','expected.json',f'{MODULE}/identity.json',*DESCRIPTOR_PATHS};result={}
 for p in sorted(root.rglob('*'),key=lambda p:p.relative_to(root).as_posix().encode()):
  if not p.is_file() or any(part in {'__pycache__','.pytest_cache','.ruff_cache','.mypy_cache'} for part in p.parts) or p.suffix=='.pyc':continue
  rel=p.relative_to(root).as_posix()
  if rel not in excluded:result[rel]=p.read_bytes()
 return result
def rebuild_package():
 root=Path(__file__).resolve().parents[1];sdk=root.parents[1]/'sdk'
 if str(sdk) not in sys.path:sys.path.insert(0,str(sdk))
 from plotpilot_plugin_sdk.package import digest_package
 _write_json(root/'plugin.json',plugin_manifest());_write_json(root/'ui'/'metadata-only.json',ui_metadata())
 schema_artifacts={}
 for cap,index_rel in INDEX_BY_CAPABILITY.items():
  path=root/index_rel;value=json.loads(path.read_text(encoding='utf8'))
  if set(value)!={'schema','plugin_id','capability_id','schemas'} or value['capability_id']!=cap:raise ValueError('schema index is not closed')
  for item in value['schemas']:
   item['sha256']=hashlib.sha256((root/MODULE/item['path']).read_bytes()).hexdigest()
  _write_json(path,value);schema_artifacts[index_rel]=value['schemas']
 wheel=build_canonical_wheel(root,root/'backend');files=_payload(root);digest=digest_package(files,PLUGIN_ID,VERSION);identity={'schema':'asset-derivation-package-identity/v1','plugin_id':PLUGIN_ID,'version':VERSION,'package_hash':digest.package_hash,'release_id':digest.release_id}
 descriptor_hashes={};descriptor_identities={}
 for rel,val in descriptor_files(digest.release_id).items():_write_json(root/rel,val);descriptor_hashes[rel]=hashlib.sha256((root/rel).read_bytes()).hexdigest();descriptor_identities[rel]=val['provider']
 (root/'files.sha256').write_bytes(digest.files_sha256);files=_payload(root);projection=capability_projection();ph=hashlib.sha256(json.dumps(projection,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 expected={'schema':'asset-derivation-package-expected/v1','plugin_id':PLUGIN_ID,'version':VERSION,'package_files':list(sorted(files,key=lambda x:x.encode())),'files_sha256':digest.files_sha256.decode(),'package_hash':digest.package_hash,'release_id':digest.release_id,'provider_identity':identity,'descriptor_paths':list(DESCRIPTOR_PATHS),'descriptor_sha256':descriptor_hashes,'descriptor_provider_identity':descriptor_identities,'schema_index_paths':INDEX_BY_CAPABILITY,'schema_index_path_list':list(INDEX_BY_CAPABILITY.values()),'schema_index_sha256':{r:hashlib.sha256((root/r).read_bytes()).hexdigest() for r in INDEX_BY_CAPABILITY.values()},'schema_artifacts':schema_artifacts,'schema_artifact_sha256':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/MODULE/'schemas').rglob('*.json'))},'wheel_path':wheel.relative_to(root).as_posix(),'wheel_import':MODULE,'plugin_path':'plugin.json','identity_path':f'{MODULE}/identity.json','capability_projection':projection,'capability_projection_sha256':ph}
 _write_json(root/'expected.json',expected);_write_json(root/MODULE/'identity.json',identity)
 from .package_identity import calculate_identity;calculate_identity(root);return expected
if __name__=='__main__':print(json.dumps({k:rebuild_package()[k] for k in ('plugin_id','package_hash','release_id','wheel_path')},sort_keys=True))
