# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


class StageError(ValueError):
    pass


class StageSizeError(StageError):
    pass


@dataclass(frozen=True)
class StagedFile:
    length: int
    sha256: str


async def stage_file(read, destination: Path, *, max_bytes: int,
                     expected_length: int | None = None,
                     expected_sha256: str | None = None,
                     reserve_bytes: int = 0) -> StagedFile:
    """Copy an async byte source to a private job file and publish on success.

    The caller owns the job directory and unique destination. Cancellation
    propagates from read(), and only the temporary file for this call is removed.
    """
    if expected_length is not None:
        if not 0 < expected_length <= max_bytes:
            raise StageSizeError("Invalid staged file length")
        if shutil.disk_usage(destination.parent).free < expected_length + reserve_bytes:
            raise StageError("Insufficient space for the staged file")
    if expected_sha256 is not None:
        if len(expected_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in expected_sha256
        ):
            raise StageError("Invalid SHA-256 digest")

    length = 0
    digest = hashlib.sha256()
    fd, name = tempfile.mkstemp(prefix=".preflight-", suffix=".part",
                                dir=destination.parent)
    pending = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                block = await read(CHUNK_SIZE)
                if not block:
                    break
                length += len(block)
                if length > max_bytes:
                    raise StageSizeError("File exceeds the staging limit")
                if expected_length is not None and length > expected_length:
                    raise StageError("Staged file exceeds its declared length")
                if reserve_bytes and shutil.disk_usage(destination.parent).free < (
                    reserve_bytes + len(block)
                ):
                    raise StageError("Insufficient space for the staged file")
                out.write(block)
                digest.update(block)
            if not length:
                raise StageError("Empty G-code file")
            if expected_length is not None and length != expected_length:
                raise StageError("Staged file does not match its declared length")
            actual = digest.hexdigest()
            if expected_sha256 is not None and not hmac.compare_digest(actual, expected_sha256):
                raise StageError("Staged file failed SHA-256 verification")
            out.flush()
            os.fsync(out.fileno())
        os.replace(pending, destination)
        return StagedFile(length, actual)
    finally:
        pending.unlink(missing_ok=True)
