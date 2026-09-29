#!/usr/bin/env python3
"""Build the allowlisted PAXX-managed multiACE archive.

The regular multiACE repository contains an SSH installer and maintenance
scripts for standalone users. Those scripts must not be shipped as the
PAXX-managed payload, because PAXX owns installation and update lifecycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).with_name("manifest.json")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _read_version(manifest: dict) -> str:
    path = ROOT / manifest["version_file"]
    version = path.read_text(encoding="utf-8").strip()
    if not version or not VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid version in {path}")
    return version


def _safe_relative(value: str, field: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid {field} path: {value}")
    return path.as_posix().rstrip("/")


def _validate_manifest(manifest: dict) -> None:
    if manifest.get("schema") != 1:
        raise ValueError("unsupported package manifest schema")
    for relative in manifest.get("payload", []):
        _safe_relative(relative, "payload")
    for relative in manifest.get("excluded_from_package", []):
        _safe_relative(relative, "excluded")
    for mount in manifest.get("klipper_mounts", []):
        _safe_relative(mount["source"], "mount source")
        if not mount["target"].startswith("/"):
            raise ValueError(f"mount target is not absolute: {mount['target']}")


def _copy_payload(stage: Path, manifest: dict) -> None:
    for relative in manifest["payload"]:
        relative = _safe_relative(relative, "payload")
        source = ROOT / relative
        if not source.exists():
            raise FileNotFoundError(f"payload entry does not exist: {relative}")
        destination = stage / relative
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    for relative in (manifest["version_file"], "LICENSE"):
        relative = _safe_relative(relative, "metadata")
        source = ROOT / relative
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    manifest_destination = stage / "paxx" / "manifest.json"
    manifest_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MANIFEST_PATH, manifest_destination)


def _assert_excluded(stage: Path, manifest: dict) -> None:
    forbidden = tuple(
        _safe_relative(item, "excluded")
        for item in manifest["excluded_from_package"]
    )
    for path in stage.rglob("*"):
        relative = path.relative_to(stage).as_posix()
        if any(relative == item or relative.startswith(item + "/")
               for item in forbidden):
            raise AssertionError(f"excluded path leaked into package: {relative}")


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def build(output: Path) -> tuple[Path, str]:
    manifest = _load_manifest()
    _validate_manifest(manifest)
    version = _read_version(manifest)
    root_name = f"multiace-{version}"
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="multiace-paxx-") as temporary:
        stage = Path(temporary) / root_name
        stage.mkdir()
        _copy_payload(stage, manifest)
        _assert_excluded(stage, manifest)

        with output.open("wb") as raw:
            import gzip

            with gzip.GzipFile(
                    filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    archive.add(stage, arcname=root_name,
                                filter=_tar_filter)

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_name(output.name + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    return output, digest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output archive path",
    )
    args = parser.parse_args(argv)
    try:
        if args.output is None:
            manifest = _load_manifest()
            version = _read_version(manifest)
            prefix = manifest["release"]["asset_prefix"]
            suffix = manifest["release"]["archive_suffix"]
            args.output = Path("dist") / f"{prefix}{version}{suffix}"
        output, digest = build(args.output)
    except (OSError, ValueError, AssertionError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created {output}")
    print(f"sha256 {digest}")
    print(f"checksum {output}.sha256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
