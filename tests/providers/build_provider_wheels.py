"""Rebuild the two deterministic provider package artifacts.

This is intentionally a standard-library PEP 427 builder: the repository
runner does not need an online build backend.  Package/release digests are
still calculated exclusively by the public PlotPilot SDK.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
if str(SDK_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SDK_ROOT))

from plotpilot_plugin_sdk.package import build_files_sha256, digest_package  # noqa: E402


PROVIDERS = (
    {
        "folder": "provider-anthropic",
        "module": "provider_anthropic",
        "plugin_id": "com.plotpilot.novelagent.provider-anthropic",
        "capability_id": "model.provider.anthropic.invoke/v1",
        "distribution": "plotpilot_provider_anthropic",
        "display_name": "Novel-Agent Anthropic Provider",
    },
    {
        "folder": "provider-gemini",
        "module": "provider_gemini",
        "plugin_id": "com.plotpilot.novelagent.provider-gemini",
        "capability_id": "model.provider.gemini.invoke/v1",
        "distribution": "plotpilot_provider_gemini",
        "display_name": "Novel-Agent Gemini Provider",
    },
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wheel_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")


def _wheel_entry(name: str, data: bytes) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    return info


def _build_wheel(config: dict[str, str], root: Path) -> Path:
    module = config["module"]
    distribution = config["distribution"]
    dist_info = f"{distribution}-0.1.0.dist-info"
    output = root / "backend" / f"{distribution}-0.1.0-py3-none-any.whl"
    files: dict[str, bytes] = {}
    package_root = root / module
    for path in sorted(package_root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix not in {".py", ".json"}:
            continue
        relative = path.relative_to(root).as_posix()
        files[relative] = path.read_bytes()

    metadata = (
        "Metadata-Version: 2.3\n"
        f"Name: {distribution.replace('_', '-')}\n"
        "Version: 0.1.0\n"
        f"Summary: {config['display_name']}\n"
        "Requires-Python: >=3.12,<3.13\n"
        "\n"
    ).encode("utf-8")
    wheel = b"Wheel-Version: 1.0\nGenerator: nap00-deterministic-wheel-builder/1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    top_level = f"{module}\n".encode("ascii")
    files[f"{dist_info}/METADATA"] = metadata
    files[f"{dist_info}/WHEEL"] = wheel
    files[f"{dist_info}/top_level.txt"] = top_level

    record_rows: list[list[str]] = []
    for name in sorted(files):
        data = files[name]
        record_rows.append([name, _wheel_hash(data), str(len(data))])
    record_rows.append([f"{dist_info}/RECORD", "", ""])
    record_buffer = io.StringIO(newline="")
    writer = csv.writer(record_buffer, lineterminator="\n")
    writer.writerows(record_rows)
    files[f"{dist_info}/RECORD"] = record_buffer.getvalue().encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            archive.writestr(_wheel_entry(name, files[name]), files[name])
    return output


def _package_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or path.name in {".gitattributes", "files.sha256", "expected.json"}
        ):
            continue
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def rebuild(config: dict[str, str]) -> dict[str, object]:
    root = ROOT / "plugins" / config["folder"]
    identity_path = root / config["module"] / "identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    digest = digest_package(
        {name: (root / name).read_bytes() for name in identity["identity_files"]},
        config["plugin_id"],
        "0.1.0",
    )
    identity["package_hash"] = digest.package_hash
    identity["release_id"] = digest.release_id
    _write_json(identity_path, identity)

    wheel = _build_wheel(config, root)
    descriptor = json.loads((root / "descriptor.json").read_text(encoding="utf-8"))
    descriptor["provider"]["release_id"] = digest.release_id
    _write_json(root / "descriptor.json", descriptor)

    files = _package_files(root)
    manifest_bytes = build_files_sha256(files)
    (root / "files.sha256").write_bytes(manifest_bytes)
    package_files = sorted([*files, "files.sha256"], key=lambda name: name.encode("utf-8"))
    expected = {
        "schema": "provider-package-expected/v1",
        "plugin_id": config["plugin_id"],
        "version": "0.1.0",
        "package_files": package_files,
        "manifest_files": sorted(files, key=lambda name: name.encode("utf-8")),
        "identity_files": sorted(identity["identity_files"], key=lambda name: name.encode("utf-8")),
        "files_sha256": manifest_bytes.decode("utf-8"),
        "package_hash": digest.package_hash,
        "release_id": digest.release_id,
        "wheel_path": wheel.relative_to(root).as_posix(),
        "wheel_import": config["module"],
        "input_schema_path": f"{config['module']}/schemas/input.schema.json",
        "output_schema_path": f"{config['module']}/schemas/output.schema.json",
        "descriptor_path": "descriptor.json",
        "plugin_path": "plugin.json",
        "schema_index_path": f"{config['module']}/schemas/index.json",
    }
    _write_json(root / "expected.json", expected)
    return expected


def main() -> None:
    for config in PROVIDERS:
        expected = rebuild(config)
        print(json.dumps({"plugin_id": config["plugin_id"], "package_hash": expected["package_hash"], "release_id": expected["release_id"], "wheel_path": expected["wheel_path"]}, sort_keys=True))


if __name__ == "__main__":
    main()
