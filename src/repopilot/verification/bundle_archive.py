"""Read and write the bounded ZIP transport for public bundles."""

import io
import stat
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from repopilot.ledger.audit import MAX_AUDIT_BYTES
from repopilot.tools.errors import ToolFailure
from repopilot.tools.policy import ReadPolicy

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 201  # 200 manifest entries plus bundle.json.


def _check_names(names: list[str]) -> None:
    seen = set()
    for name in names:
        path = PurePosixPath(name)
        folded = name.casefold()
        if (not path.parts or path.is_absolute() or path.as_posix() != name or folded in seen
                or any(char in name for char in ("\\", ":", "\x00"))
                or any(part == ".." or part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()
                       for part in path.parts)):
            raise ToolFailure("BUNDLE_ARCHIVE_PATH", "ZIP entries must have unique canonical relative file paths")
        seen.add(folded)
    if any(parent.as_posix().casefold() in seen for name in names for parent in PurePosixPath(name).parents):
        raise ToolFailure("BUNDLE_ARCHIVE_PATH", "ZIP entries cannot use a file as a directory")


def _check_sizes(sizes: list[int]) -> None:
    if (len(sizes) > MAX_ARCHIVE_ENTRIES or any(size > MAX_AUDIT_BYTES for size in sizes)
            or sum(sizes) > MAX_ARCHIVE_BYTES):
        raise ToolFailure("BUNDLE_ARCHIVE_LIMIT", "ZIP entry count or expanded size exceeds the bundle limit")


def read_archive(path: Path) -> dict[str, bytes]:
    path = path.absolute()
    for node in (*reversed(path.parents), path):
        ReadPolicy._check_node(node)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ToolFailure("BUNDLE_ARCHIVE_INVALID", "Expected a regular ZIP file")
    if info.st_size > MAX_ARCHIVE_BYTES:
        raise ToolFailure("BUNDLE_ARCHIVE_LIMIT", "ZIP file exceeds the bundle limit")
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            _check_sizes([entry.file_size for entry in entries])
            _check_names([entry.orig_filename for entry in entries])
            files = {}
            for entry in entries:
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (kind not in {0, stat.S_IFREG} or entry.external_attr & 0x410 or entry.flag_bits & 1
                        or entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED}):
                    raise ToolFailure("BUNDLE_ARCHIVE_INVALID", "ZIP entries must be unencrypted regular files")
                with archive.open(entry) as stream:
                    data = stream.read(entry.file_size + 1)
                if len(data) != entry.file_size:
                    raise ToolFailure("BUNDLE_ARCHIVE_INVALID", "ZIP entry length differs from its record")
                files[entry.filename] = data
            return files
    except (BadZipFile, EOFError, NotImplementedError, UnicodeError, zlib.error) as exc:
        raise ToolFailure("BUNDLE_ARCHIVE_INVALID", "Cannot read the bundle ZIP") from exc


def encode_archive(files: dict[str, bytes]) -> bytes:
    _check_names(list(files))
    _check_sizes([len(data) for data in files.values()])
    buffer = io.BytesIO()
    with ZipFile(buffer, mode="w") as archive:
        for name, data in sorted(files.items()):
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
    result = buffer.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise ToolFailure("BUNDLE_ARCHIVE_LIMIT", "ZIP file exceeds the bundle limit")
    return result
