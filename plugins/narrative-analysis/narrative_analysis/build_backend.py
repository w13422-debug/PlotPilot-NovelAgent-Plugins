"""Deterministic PEP 517 wheel builder and outer package identity binder."""
from __future__ import annotations
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

from .capability_spec import (
    CAPABILITY_SPECS, DESCRIPTOR_PATHS, INDEX_BY_CAPABILITY, PLUGIN_ID,
    VERSION, capability_projection, descriptor_files, plugin_manifest,
    ui_metadata,
)

NAME = "plotpilot_narrative_analysis"
MODULE = "narrative_analysis"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"
WHEEL_NAME = f"{NAME}-{VERSION}-py3-none-any.whl"

def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980,1,1,0,0,0)); info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3; info.external_attr = 0o644 << 16; return info

def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")

def _metadata() -> dict[str, bytes]:
    return {f"{DIST_INFO}/METADATA": ("Metadata-Version: 2.3\nName: plotpilot-narrative-analysis\nVersion: 0.1.0\nSummary: Evidence-closed narrative unit plan and synthesis Code Plugin\nRequires-Python: >=3.12,<3.13\n\n").encode(),
            f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\nGenerator: nap00-deterministic-wheel-builder/1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{DIST_INFO}/top_level.txt": b"narrative_analysis\n"}

def build_canonical_wheel(package_root: Path, wheel_directory: Path) -> Path:
    files: dict[str,bytes] = {}
    # Include the complete package sidecar set so the installed wheel can
    # replay identity independently of a source checkout.  Exclude generated
    # wheels from both the archive and digest to avoid recursive archives.
    for path in sorted(package_root.rglob("*"), key=lambda p:p.relative_to(package_root).as_posix().encode()):
        if (path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts
                and path.suffix != ".whl"):
            files[path.relative_to(package_root).as_posix()] = path.read_bytes()
    files.update(_metadata()); rows=[]
    for name in sorted(files, key=lambda v:v.encode()):
        data=files[name]; digest=base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        rows.append([name,"sha256="+digest,str(len(data))])
    rows.append([f"{DIST_INFO}/RECORD","",""]); out=io.StringIO(newline="")
    csv.writer(out,lineterminator="\n").writerows(rows); files[f"{DIST_INFO}/RECORD"]=out.getvalue().encode()
    wheel_directory.mkdir(parents=True,exist_ok=True); output=wheel_directory/WHEEL_NAME
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in sorted(files,key=lambda v:v.encode()): archive.writestr(_entry(name),files[name])
    return output

def build_wheel(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    return build_canonical_wheel(Path(__file__).resolve().parents[1],Path(wheel_directory)).name

def prepare_metadata_for_build_wheel(metadata_directory: str, config_settings=None) -> str:
    target=Path(metadata_directory)/DIST_INFO;target.mkdir(parents=True,exist_ok=True)
    for name,data in _metadata().items(): (target/name.split("/",1)[1]).write_bytes(data)
    return DIST_INFO

def _payload(root: Path) -> dict[str,bytes]:
    excluded={"files.sha256","expected.json",f"{MODULE}/identity.json",".gitattributes",*DESCRIPTOR_PATHS,"backend/plotpilot_narrative_analysis-0.1.0-py3-none-any.whl"}
    result={}
    for path in sorted(root.rglob("*"),key=lambda p:p.relative_to(root).as_posix().encode()):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix==".pyc": continue
        rel=path.relative_to(root).as_posix()
        if rel not in excluded: result[rel]=path.read_bytes()
    return result

def rebuild_package() -> dict[str,object]:
    root=Path(__file__).resolve().parents[1]; sdk=root.parents[1]/"sdk"
    if str(sdk) not in sys.path: sys.path.insert(0,str(sdk))
    from plotpilot_plugin_sdk.package import digest_package
    _write_json(root/"plugin.json", plugin_manifest())
    _write_json(root/"ui"/"metadata-only.json", ui_metadata())
    schema_artifacts={}
    for capability,index_rel in INDEX_BY_CAPABILITY.items():
        path=root/index_rel; value=json.loads(path.read_text(encoding="utf-8"))
        if set(value)!={"schema","plugin_id","capability_id","schemas"} or value["capability_id"]!=capability: raise ValueError("schema index is not closed")
        for item in value["schemas"]:
            schema_path=root/MODULE/item["path"]; item["sha256"]=hashlib.sha256(schema_path.read_bytes()).hexdigest()
        _write_json(path,value); schema_artifacts[index_rel]=value["schemas"]
    digest=digest_package(_payload(root),PLUGIN_ID,VERSION)
    identity={"schema":"narrative-analysis-package-identity/v1","plugin_id":PLUGIN_ID,"version":VERSION,"package_hash":digest.package_hash,"release_id":digest.release_id}
    _write_json(root/MODULE/"identity.json",identity)
    descriptor_hashes={}; descriptor_identities={}
    for rel, value in descriptor_files(digest.release_id).items():
        path=root/rel; _write_json(path,value)
        descriptor_hashes[rel]=hashlib.sha256(path.read_bytes()).hexdigest();descriptor_identities[rel]=value["provider"]
    (root/"files.sha256").write_bytes(digest.files_sha256)
    physical=[]
    for path in root.rglob("*"):
        if (path.is_file() and "__pycache__" not in path.parts and path.suffix!=".pyc"
                and path.suffix != ".whl" and path.name not in {".gitattributes","expected.json","files.sha256"}):
            physical.append(path.relative_to(root).as_posix())
    indexes=list(INDEX_BY_CAPABILITY.values())
    projection=capability_projection()
    expected={"schema":"narrative-analysis-package-expected/v1","plugin_id":PLUGIN_ID,"version":VERSION,
        "package_files":sorted([*physical,"files.sha256"],key=lambda v:v.encode()),"files_sha256":digest.files_sha256.decode(),
        "package_hash":digest.package_hash,"release_id":digest.release_id,"provider_identity":identity,
        "capability_projection":projection,
        "capability_projection_sha256":hashlib.sha256(json.dumps(projection,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode("utf-8")).hexdigest(),
        "descriptor_paths":list(DESCRIPTOR_PATHS),"descriptor_sha256":descriptor_hashes,"descriptor_provider_identity":descriptor_identities,
        "schema_index_paths":INDEX_BY_CAPABILITY,"schema_index_path_list":indexes,
        "schema_index_sha256":{rel:hashlib.sha256((root/rel).read_bytes()).hexdigest() for rel in indexes},
        "schema_artifacts":schema_artifacts,
        "schema_artifact_sha256":{path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((root/MODULE/"schemas").rglob("*.schema.json"))},
        "wheel_path":f"backend/{WHEEL_NAME}","wheel_import":MODULE,"plugin_path":"plugin.json","identity_path":f"{MODULE}/identity.json"}
    _write_json(root/"expected.json",expected)
    # Build after all identity/manifest/expected sidecars have final bytes.
    wheel=build_canonical_wheel(root,root/"backend")
    from narrative_analysis.package_identity import calculate_identity
    verified = calculate_identity(root)
    if verified.package_hash != digest.package_hash or verified.release_id != digest.release_id:
        raise ValueError("rebuilt narrative-analysis package identity did not verify")
    return expected

if __name__=="__main__":
    value=rebuild_package();print(json.dumps({key:value[key] for key in ("plugin_id","package_hash","release_id","wheel_path")},sort_keys=True))
