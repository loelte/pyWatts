"""Find and read activities in files, folders and zip archives. Nothing is extracted to disk."""

from __future__ import annotations

import gzip
import hashlib
import io
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pywatts.storage.repo import ImportedRide

MAX_FILE_BYTES = 64 * 1024 * 1024  # uncompressed
_FIT = (".fit", ".fit.gz")
_OTHER = (".tcx", ".gpx", ".tcx.gz", ".gpx.gz")


@dataclass(frozen=True)
class FileResult:
    """One activity: a ride, or the reason there is none."""

    name: str
    sha256: str = ""
    ride: ImportedRide | None = None
    source: str = "file"
    skipped: bool = False  # readable or known format, but not wanted
    error: str = ""

    @property
    def external_id(self) -> str:
        return f"sha256:{self.sha256}"  # cannot collide with ids of a service


def read_files(paths: Iterable[Path]) -> Iterator[FileResult]:
    """Lazy; a bad file yields an error and the rest goes on."""
    for path in paths:
        try:
            if path.is_dir():
                yield from read_files(sorted(p for p in path.rglob("*") if p.is_file()))
            elif path.suffix.lower() == ".zip":
                yield from _read_zip(path)
            elif _wanted(path.name):
                with path.open("rb") as stream:
                    yield _read(path.name, stream)
        except Exception as e:
            yield FileResult(path.name, error=f"{type(e).__name__}: {e}")


def _read_zip(path: Path | IO[bytes]) -> Iterator[FileResult]:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename
            if info.is_dir() or not _wanted(name):
                continue
            try:
                with archive.open(info) as stream:
                    yield _read(name, stream)
            except Exception as e:
                yield FileResult(name, error=f"{type(e).__name__}: {e}")


def _wanted(name: str) -> bool:
    return name.lower().endswith(_FIT + _OTHER)


def _read(name: str, stream: IO[bytes]) -> FileResult:
    if name.lower().endswith(_OTHER):
        return FileResult(name, skipped=True, error="only FIT files can be imported")
    try:
        data = _limited(stream)
        if name.lower().endswith(".gz"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as unzipped:
                data = _limited(unzipped)
    except (OSError, EOFError, RuntimeError, ValueError, zipfile.BadZipFile) as e:
        return FileResult(name, error=str(e))
    return read_bytes(name, data)


def read_archive(name: str, data: bytes) -> FileResult:
    """The ride in a zip archive, or in a plain FIT file."""
    if not data.startswith(b"PK"):
        return read_bytes(name, data)
    first: FileResult | None = None
    try:
        for result in _read_zip(io.BytesIO(data)):
            if result.ride is not None:
                return result
            first = first or result
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        return FileResult(name, error=str(e))
    return first or FileResult(name, error="there is no FIT file in the archive")


def read_bytes(name: str, data: bytes) -> FileResult:
    from pywatts.storage.fit_import import (
        GARMIN,
        WAHOO,
        ZWIFT,
        NotCycling,
        parse_fit,
    )  # slow to load

    digest = hashlib.sha256(data).hexdigest()
    try:
        ride = parse_fit(data)
    except NotCycling as e:
        return FileResult(name, digest, skipped=True, error=str(e))
    except ValueError as e:
        return FileResult(name, digest, error=str(e))
    source = {ZWIFT: "zwift", WAHOO: "wahoo", GARMIN: "garmin"}.get(ride.manufacturer, "file")
    return FileResult(name, digest, ride, source)


def _limited(stream: IO[bytes]) -> bytes:
    data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("the file is too large")
    return data
