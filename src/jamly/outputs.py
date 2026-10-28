"""Publish complete track directories, with rollback and interrupted-swap recovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

PROCESSING_VERSION = 2


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(directory: Path, *, verify_hashes: bool = True) -> dict:
    data = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid output manifest")
    if data.get("processing_version") != PROCESSING_VERSION:
        raise ValueError("outputs were made by a different processing version")
    if not isinstance(data.get("generation"), str) or not data["generation"]:
        raise ValueError("output generation is missing")
    if not isinstance(data.get("outputs"), dict) or set(data["outputs"]) != {"drums", "no_drums"}:
        raise ValueError("the output pair is incomplete")
    for record in data["outputs"].values():
        if not isinstance(record, dict):
            raise ValueError("invalid output record")
        name = record.get("file")
        if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
            raise ValueError("invalid output filename")
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("missing output: %s" % path)
        if verify_hashes and checksum(path) != record.get("sha256"):
            raise ValueError("output checksum mismatch: %s" % path)
    return data


def previous_directory(destination: Path) -> Path:
    return destination.with_name("." + destination.name + ".previous")


def recover_directory(destination: Path) -> None:
    backup = previous_directory(destination)
    if not backup.exists():
        return
    if not destination.exists():
        os.replace(backup, destination)
    else:
        read_manifest(destination)
        shutil.rmtree(backup)


def publish_directory(staged: Path, destination: Path) -> None:
    """The caller holds the track/format lock. Readers see a whole generation or no directory."""
    read_manifest(staged)
    for path in staged.iterdir():
        if path.is_file():
            # Windows' FlushFileBuffers requires a handle opened for writing.
            with path.open("rb+") as file:
                os.fsync(file.fileno())
    backup = previous_directory(destination)
    recover_directory(destination)
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except OSError:
        if backup.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)
