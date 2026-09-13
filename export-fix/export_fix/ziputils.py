"""Zip file I/O helpers."""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple


def _set_zip_permissions(zi: zipfile.ZipInfo, is_dir: bool = False) -> None:
    """Set Unix permissions: 755 for dirs, 644 for files."""
    perms = 0o755 if is_dir else 0o644
    zi.external_attr = (perms << 16) | (0x10 if is_dir else 0)


def _normalize_zip_path(p: str) -> str:
    # Zip files use forward slashes
    return re.sub(r"[\\]+", "/", p).lstrip("./")


def _ensure_zip_parent_dirs(
    zf: zipfile.ZipFile,
    file_path: str,
    date_time: Tuple[int, int, int, int, int, int],
    emitted_dirs: Optional[set] = None,
) -> None:
    # Emit all parent directory entries for a given file path
    norm = _normalize_zip_path(file_path)
    parts = norm.split("/")
    acc = ""
    for i in range(len(parts) - 1):
        acc = f"{acc}{parts[i]}/"
        if emitted_dirs is not None:
            if acc in emitted_dirs:
                continue
            emitted_dirs.add(acc)
        zi = zipfile.ZipInfo(acc, date_time)
        _set_zip_permissions(zi, is_dir=True)
        zf.writestr(zi, b"")


def _is_zip(path: str) -> bool:
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with zipfile.ZipFile(str(p)) as _:
            return True
    except zipfile.BadZipFile:
        return False


def _strip_common_dir_prefix(paths: List[str]) -> str:
    """
    Get a common directory prefix stripped from all given paths.

    Returns only a complete directory prefix (e.g. 'Export-<uuid>/') that is
    shared by every path; otherwise returns an empty string.
    """
    if not paths:
        return ""
    common_prefix = os.path.commonprefix(paths)
    if (
        common_prefix
        and common_prefix.endswith("/")
        and all(f.startswith(common_prefix) for f in paths)
    ):
        return common_prefix
    return ""


def _read_nested_zip(nested_zip_name: str, nested_zip_data: bytes) -> List[str]:
    """Get file list of a nested zip from its raw bytes."""
    with zipfile.ZipFile(io.BytesIO(nested_zip_data)) as inner_zf:
        return [info.filename for info in inner_zf.infolist() if not info.is_dir()]


def _is_export_wrapper(nested_zip_name: str, nested_zip_data: bytes) -> bool:
    """
    Detect whether a nested zip is a Notion export wrapper zip rather than an
    embedded file attachment (e.g. a user-uploaded zip inside a page).

    Notion export wrappers are either at the root of the outer zip or inside
    a directory named after themselves (e.g. 'Export-<uuid>/Export-<uuid>.zip')
    and contain markdown/csv export content. Embedded attachments live in
    page directories with unrelated names.
    """
    try:
        nested_files = _read_nested_zip(nested_zip_name, nested_zip_data)
    except Exception:
        return False

    # Wrapper zips contain the actual export content (.md pages / .csv data)
    if not any(f.lower().endswith((".md", ".csv")) for f in nested_files):
        return False

    # Location check: root of the outer zip, or in a directory with the same name
    parent = os.path.dirname(nested_zip_name)
    parent_name = os.path.basename(parent) if parent else ""
    stem = os.path.basename(nested_zip_name)
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    return (not parent_name) or (parent_name.lower() == stem.lower())
