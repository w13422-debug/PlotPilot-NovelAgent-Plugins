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

from plotpilot_plugin_sdk.package import digest_package  # noqa: E402


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
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or path.suffix not in {".py", ".json"}
            or path.name == "identity.json"
        ):
            # identity.json is generated metadata.  It must remain a sidecar:
            # embedding its package/release digest in the Wheel would make the
            # Wheel self-referential because the Wheel is payload.
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


def _canonical_payload_files(root: Path, module: str) -> dict[str, bytes]:
    """Build the one payload map whose names are committed in files.sha256."""
    generated_metadata = {"descriptor.json", "files.sha256", "expected.json", f"{module}/identity.json"}
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        if relative in generated_metadata or path.name == ".gitattributes":
            continue
        files[relative] = path.read_bytes()
    return files


def _manifest_names(manifest_bytes: bytes) -> tuple[str, ...]:
    text = manifest_bytes.decode("utf-8")
    names: list[str] = []
    for line in text.splitlines(keepends=True):
        if "  " not in line or not line.endswith("\n"):
            raise ValueError("generated files.sha256 contains a malformed line")
        name = line[:-1].split("  ", 1)[1]
        if name in names:
            raise ValueError("generated files.sha256 contains a duplicate path")
        names.append(name)
    return tuple(names)


def rebuild(config: dict[str, str]) -> dict[str, object]:
    root = ROOT / "plugins" / config["folder"]
    module = config["module"]

    # Build the installable Wheel before calculating identity.  The Wheel is a
    # canonical payload member, but generated identity metadata is not embedded
    # in it and therefore cannot create a hash fixed-point cycle.
    wheel = _build_wheel(config, root)
    canonical_files = _canonical_payload_files(root, module)
    digest = digest_package(canonical_files, config["plugin_id"], "0.1.0")
    manifest_names = _manifest_names(digest.files_sha256)
    if set(manifest_names) != set(canonical_files):
        raise ValueError("public SDK manifest does not describe the canonical payload map")
    if any(name in manifest_names for name in {"descriptor.json", "files.sha256", f"{module}/identity.json"}):
        raise ValueError("generated identity/descriptor metadata must remain outside the payload")
    if wheel.relative_to(root).as_posix() not in manifest_names:
        raise ValueError("installable Wheel is missing from the canonical payload")

    identity_path = root / module / "identity.json"
    identity = {
        "schema": "provider-package-identity/v1",
        "plugin_id": config["plugin_id"],
        "version": "0.1.0",
        "package_hash": digest.package_hash,
        "release_id": digest.release_id,
    }
    _write_json(identity_path, identity)

    descriptor_path = root / "descriptor.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor["provider"]["release_id"] = digest.release_id
    _write_json(descriptor_path, descriptor)

    # package_files is only the physical bundle inventory used by the gate;
    # files.sha256 is the sole canonical payload map.
    files = _package_files(root)
    manifest_bytes = digest.files_sha256
    (root / "files.sha256").write_bytes(manifest_bytes)
    package_files = sorted([*files, "files.sha256"], key=lambda name: name.encode("utf-8"))
    expected = {
        "schema": "provider-package-expected/v1",
        "plugin_id": config["plugin_id"],
        "version": "0.1.0",
        "package_files": package_files,
        "files_sha256": manifest_bytes.decode("utf-8"),
        "descriptor_sha256": _sha256(descriptor_path.read_bytes()),
        "package_hash": digest.package_hash,
        "release_id": digest.release_id,
        "wheel_path": wheel.relative_to(root).as_posix(),
        "wheel_import": module,
        "input_schema_path": f"{module}/schemas/input.schema.json",
        "output_schema_path": f"{module}/schemas/output.schema.json",
        "descriptor_path": "descriptor.json",
        "plugin_path": "plugin.json",
        "schema_index_path": f"{module}/schemas/index.json",
    }
    _write_json(root / "expected.json", expected)
    return expected

def main() -> None:
    for config in PROVIDERS:
        expected = rebuild(config)
        print(json.dumps({"plugin_id": config["plugin_id"], "package_hash": expected["package_hash"], "release_id": expected["release_id"], "wheel_path": expected["wheel_path"]}, sort_keys=True))


if __name__ == "__main__":
    main()
