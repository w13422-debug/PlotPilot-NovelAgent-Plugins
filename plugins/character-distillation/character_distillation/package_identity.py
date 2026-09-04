"""Fail-closed identity verifier for the Character Distillation package."""
from __future__ import annotations
import hashlib,json,re
from pathlib import Path
import sys
_sdk=next((parent/'sdk' for parent in Path(__file__).resolve().parents if (parent/'sdk').exists()),None)
if _sdk is not None and str(_sdk) not in sys.path: sys.path.insert(0,str(_sdk))
from .capability_spec import CAPABILITY_SPECS,DESCRIPTOR_PATHS,PLUGIN_ID,VERSION,capability_projection,plugin_manifest,ui_metadata
MODULE_NAME='character_distillation';IDENTITY_SCHEMA='character-distillation-package-identity/v1';HASH_RE=re.compile(r'^[0-9a-f]{64}$')
def package_root():return Path(__file__).resolve().parents[1]
def identity_path(root:Path|None=None):return (root or package_root())/MODULE_NAME/'identity.json'
def _json(path):
 def dup(pairs):
  d={}
  for k,v in pairs:
   if k in d:raise ValueError('duplicate key')
   d[k]=v
  return d
 try:v=json.loads(path.read_text(encoding='utf8'),object_pairs_hook=dup,parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
 except Exception as exc:raise RuntimeError(f'character package JSON unavailable: {path}') from exc
 if not isinstance(v,dict):raise RuntimeError('package JSON must be object')
 return v
def _identity(root):
 v=_json(identity_path(root))
 if set(v)!={'schema','plugin_id','version','package_hash','release_id'} or v['schema']!=IDENTITY_SCHEMA or v['plugin_id']!=PLUGIN_ID or v['version']!=VERSION or any(not isinstance(v[x],str) or HASH_RE.fullmatch(v[x]) is None or v[x]=='0'*64 for x in ('package_hash','release_id')):raise RuntimeError('character package identity schema/owner/hash invalid')
 return v
def _manifest(root):
 p=root/'files.sha256'
 try:raw=p.read_bytes();text=raw.decode('utf8')
 except Exception as exc:raise RuntimeError('character files.sha256 unreadable') from exc
 if raw.startswith(b'\xef\xbb\xbf') or b'\r' in raw or not text.endswith('\n') or text.endswith('\n\n'):raise RuntimeError('character files.sha256 must be UTF-8 LF final newline')
 names=[]
 for line in text.splitlines(keepends=True):
  m=re.fullmatch(r'([0-9a-f]{64})  (.+)\n',line)
  if not m:raise RuntimeError('character files.sha256 line malformed')
  name=m.group(2)
  if name in names or name=='files.sha256':raise RuntimeError('character files.sha256 duplicate/self')
  names.append(name)
 if names!=sorted(names,key=lambda x:x.encode()):raise RuntimeError('character files.sha256 not UTF-8 sorted')
 return names,raw
def _files(root,names):
 out={}
 for name in names:
  path=root/name
  try:path.resolve().relative_to(root.resolve())
  except ValueError as exc:raise RuntimeError('manifest path escapes package') from exc
  if not path.is_file():raise RuntimeError('manifest file missing: '+name)
  out[name]=path.read_bytes()
 return out
def calculate_identity(root:Path|None=None):
 root=root or package_root();identity=_identity(root);names,manifest=_manifest(root);files=_files(root,names)
 from plotpilot_plugin_sdk.package import digest_package
 digest=digest_package(files,PLUGIN_ID,VERSION)
 if digest.files_sha256!=manifest or digest.package_hash!=identity['package_hash'] or digest.release_id!=identity['release_id']:raise RuntimeError('character package digest/identity mismatch')
 expected=_json(root/'expected.json');required={'schema','plugin_id','version','package_files','files_sha256','package_hash','release_id','provider_identity','descriptor_paths','descriptor_sha256','descriptor_provider_identity','schema_index_paths','schema_index_path_list','schema_index_sha256','schema_artifacts','schema_artifact_sha256','wheel_path','wheel_import','plugin_path','identity_path','capability_projection','capability_projection_sha256'}
 if set(expected)!=required or expected['schema']!='character-distillation-package-expected/v1' or expected['package_files']!=names or expected['files_sha256']!=manifest.decode('utf8') or expected['package_hash']!=digest.package_hash or expected['release_id']!=digest.release_id or expected['provider_identity']!=identity:raise RuntimeError('character expected identity mismatch')
 if _json(root/'plugin.json')!=plugin_manifest() or _json(root/'ui/metadata-only.json')!=ui_metadata():raise RuntimeError('character manifest/UI differs from specification')
 projection=capability_projection();ph=hashlib.sha256(json.dumps(projection,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 if expected['capability_projection']!=projection or expected['capability_projection_sha256']!=ph:raise RuntimeError('character capability projection drift')
 if expected['descriptor_paths']!=list(DESCRIPTOR_PATHS):raise RuntimeError('character descriptor order drift')
 for spec in CAPABILITY_SPECS:
  rel=spec.descriptor_path;d=_json(root/rel);expected_d=spec.descriptor(digest.release_id)
  if d!=expected_d or expected['descriptor_provider_identity'].get(rel)!=expected_d['provider'] or hashlib.sha256((root/rel).read_bytes()).hexdigest()!=expected['descriptor_sha256'].get(rel):raise RuntimeError('character descriptor identity mismatch')
 for rel in expected['schema_index_path_list']:
  idx=_json(root/rel)
  if hashlib.sha256((root/rel).read_bytes()).hexdigest()!=expected['schema_index_sha256'].get(rel) or idx.get('schemas')!=expected['schema_artifacts'].get(rel):raise RuntimeError('character schema index identity mismatch')
  for item in idx['schemas']:
   artifact=root/MODULE_NAME/item['path']
   if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest()!=item['sha256'] or expected['schema_artifact_sha256'].get(artifact.relative_to(root).as_posix())!=item['sha256']:raise RuntimeError('character schema artifact identity mismatch')
 if not (root/expected['wheel_path']).is_file():raise RuntimeError('character wheel missing')
 return digest
def load_runtime_identity():
 # A source checkout carries the outer manifest and expected sidecars; verify
 # the complete package before allowing a worker to run.  An installed wheel
 # intentionally contains only the immutable module payload plus the copied
 # outer identity sidecar (the outer manifest is not wheel content), so use
 # the same identity read without attempting to read files that are absent.
 root=package_root();value=_identity(root)
 if (root/'files.sha256').is_file(): calculate_identity(root)
 return value
