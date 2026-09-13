"""Filename sanitization and Notion ID handling."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, List, Optional

ID_SUFFIX_RE = re.compile(r"(.+?)\s([0-9a-f]{32})$", re.IGNORECASE)
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def _extract_short_id_from_path(path: str) -> str:
    """Extract a deterministic short hash from a Notion ID embedded in the path."""
    parts = re.split(r"[\\/]", path)
    for part in parts:
        name_no_ext = os.path.splitext(part)[0]
        m = ID_SUFFIX_RE.search(name_no_ext)
        if m:
            return hashlib.sha256(m.group(2).encode()).hexdigest()[:8]
        if re.match(r"^[0-9a-f]{32}$", name_no_ext, re.IGNORECASE):
            return hashlib.sha256(name_no_ext.lower().encode()).hexdigest()[:8]
        all_match = re.search(r"([0-9a-f]{32})_all", name_no_ext, re.IGNORECASE)
        if all_match:
            return hashlib.sha256(all_match.group(1).encode()).hexdigest()[:8]
    return ""


class NotionExportRenamer:
    """
    State holder for renames (ID stripping).
    Collision handling is performed later in a two-pass pipeline.
    """

    def __init__(self, filename_too_long_tracker: Optional[List] = None):
        # path -> new_name
        self._rename_cache: Dict[str, str] = {}
        self._filename_too_long_tracker = filename_too_long_tracker

    def _sanitize_name(self, name: str, original_path: str = "") -> str:
        original_name = name
        name = INVALID_FILENAME_CHARS.sub(" ", name).strip()
        if len(name) > 200:
            if self._filename_too_long_tracker is not None:
                self._filename_too_long_tracker.append(
                    (original_path, original_name, len(original_name))
                )
            name = name[:200]
        # Collapse multiple spaces
        name = re.sub(r"\s{2,}", " ", name)
        return name

    def _rewrite_single_basename(self, path_to_rename: str) -> str:
        """
        Rewrites just the basename of a file/dir by removing Notion IDs.
        """
        if path_to_rename in self._rename_cache:
            return self._rename_cache[path_to_rename]

        path, name = os.path.split(path_to_rename)
        name_no_ext, ext = os.path.splitext(name)

        # Check if the entire name is just a 32-char hex ID (for directories)
        if re.match(r"^[0-9a-f]{32}$", name_no_ext, re.IGNORECASE):
            new_name_no_ext = ""
        else:
            # First, try the existing regex for trailing space + hex
            id_match = ID_SUFFIX_RE.search(name_no_ext)
            if id_match:
                base_part = id_match.group(1)
                new_name_no_ext = base_part
            else:
                # For _all files, remove hex ID before _all
                all_match = re.search(
                    r"(.+?)([0-9a-f]{32})_all(.*)$", name_no_ext, re.IGNORECASE
                )
                if all_match:
                    base_part = all_match.group(1)
                    suffix_part = all_match.group(3)
                    new_name_no_ext = base_part + "_all" + suffix_part
                else:
                    new_name_no_ext = name_no_ext

        # Sanitize
        new_name_no_ext = self._sanitize_name(new_name_no_ext, path_to_rename)

        result = f"{new_name_no_ext}{ext}"
        self._rename_cache[path_to_rename] = result
        return result

    def rename_path(self, path_to_rename: str) -> str:
        parts = re.split(r"[\\/]", path_to_rename)
        paths = [os.path.join(*parts[0 : rpc + 1]) for rpc in range(len(parts))]
        renamed_parts = [self._rewrite_single_basename(p) for p in paths]
        # Filter out empty parts (from hex-only directory names)
        filtered_parts = [p for p in renamed_parts if p]
        return os.path.join(*filtered_parts) if filtered_parts else ""
