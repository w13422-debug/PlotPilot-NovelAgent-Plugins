"""Deterministic dependency-free PEP 517 wheel builder for source-import."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
from pathlib import Path
import zipfile

NAME = "plotpilot_source_import"
VERSION = "0.1.0"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"
WHEEL_NAME = f"{NAME}-{VERSION}-py3-none-any.whl"
WHEEL_GENERATOR = "nap00-deterministic-wheel-builder/1.0"


def _entry(name: str, data: bytes) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    return info


def _metadata_files() -> dict[str, bytes]:
    return {
        f"{DIST_INFO}/METADATA": (
            "Metadata-Version: 2.3\nName: plotpilot-source-import\nVersion: 0.1.0\n"
            "Summary: Deterministic Core-owned TXT EPUB paste source importer\n"
            "Requires-Python: >=3.12,<3.13\n\n"
        ).encode("utf-8"),
        f"{DIST_INFO}/WHEEL": (
            f"Wheel-Version: 1.0\nGenerator: {WHEEL_GENERATOR}\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode("ascii"),
        f"{DIST_INFO}/top_level.txt": b"source_import\n",
    }


def _install_payload_files(package_root: Path) -> dict[str, bytes]:
    module_root = package_root / "source_import"
    files: dict[str, bytes] = {}
    for path in sorted(module_root.rglob("*")):
        if (
            path.is_file()
            and path.suffix in {".py", ".json"}
            and path.name != "identity.json"
            and "__pycache__" not in path.parts
        ):
            files[path.relative_to(package_root).as_posix()] = path.read_bytes()
    return files


def _record(files: dict[str, bytes]) -> bytes:
    rows: list[list[str]] = []
    for name in sorted(files):
        digest = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).decode("ascii").rstrip("=")
        rows.append([name, "sha256=" + digest, str(len(files[name]))])
    rows.append([f"{DIST_INFO}/RECORD", "", ""])
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode("utf-8")


def build_canonical_wheel(package_root: Path, wheel_directory: Path) -> Path:
    """Build the one canonical wheel used by both PEP 517 and release rebuilds."""
    package_root = Path(package_root)
    wheel_directory = Path(wheel_directory)
    files = _install_payload_files(package_root)
    files.update(_metadata_files())
    files[f"{DIST_INFO}/RECORD"] = _record(files)
    output = wheel_directory / WHEEL_NAME
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            archive.writestr(_entry(name, files[name]), files[name])
    return output


def build_wheel(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    package_root = Path(__file__).resolve().parents[1]
    return build_canonical_wheel(package_root, Path(wheel_directory)).name


def prepare_metadata_for_build_wheel(metadata_directory: str, config_settings=None) -> str:
    target = Path(metadata_directory) / DIST_INFO
    target.mkdir(parents=True, exist_ok=True)
    metadata = _metadata_files()
    for name, data in metadata.items():
        if name.startswith(f"{DIST_INFO}/"):
            (target / name.split("/", 1)[1]).write_bytes(data)
    return DIST_INFO
