#!/usr/bin/env python3
"""
Export Fix - Modern Notion export enhancer compatible with Python 3.13+

Features:
- Works with a Notion export .zip or an already-extracted directory
- Removes trailing 32-hex Notion IDs from filenames and directories
- Handles naming collisions by appending " (i)"
- Produces a new .zip <input>.fixed.zip

CLI:
  export_fix.py [--notion-api-token TOKEN] [--output-path PATH] [--dest-dir PATH]
                [--remove-title] [--no-rewrite-paths] [--dont-move-md-to-folder]
                [--log-level LEVEL]
                <zip_or_dir_path>

Dependencies:
- Python 3.13+
- notion-client (optional, only used if --notion-api-token provided)
- tenacity (optional, only used when calling Notion API)
- emoji (optional, improves single-emoji detection)

Install (pip + venv example):
  python -m venv .venv
  .venv\\Scripts\\activate  (Windows)
  pip install --upgrade pip
  pip install -r requirements.txt

"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
import shutil

# Optional deps: import guarded
try:
    from notion_client import Client as NotionClient  # type: ignore
except Exception:  # pragma: no cover - optional
    NotionClient = None  # type: ignore

try:
    from tenacity import retry, stop_after_attempt, wait_exponential  # type: ignore
except Exception:  # pragma: no cover - optional
    def retry(*args, **kwargs):  # type: ignore
        def deco(fn):
            return fn
        return deco
    def stop_after_attempt(*args, **kwargs):  # type: ignore
        return None
    def wait_exponential(*args, **kwargs):  # type: ignore
        return None

try:
    import emoji as emoji_lib  # type: ignore
except Exception:  # pragma: no cover - optional
    emoji_lib = None  # type: ignore


ID_SUFFIX_RE = re.compile(r"(.+?)\s([0-9a-f]{32})$", re.IGNORECASE)
MD_LINK_OR_IMAGE_RE = re.compile(
    r"!?\[.+?\]\(([\w\d\-._~:/?=#%\]\[@!$&'\(\)*+,;]+?)\)"
)
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


@dataclass
class RenameResult:
    new_name: str
    created_time: Optional[datetime]
    last_edited_time: Optional[datetime]


class EmojiHelper:
    @staticmethod
    def is_single_emoji(text: str) -> bool:
        """
        Returns True if text looks like a single emoji character.
        Uses 'emoji' library if available; otherwise falls back to a basic heuristic.
        """
        if not text:
            return False
        if emoji_lib is not None:
            # demojize returns same text if not emoji; ensure single codepoint-ish
            demojized = emoji_lib.demojize(text)
            # A single emoji typically demojizes to :something:
            return demojized.startswith(":") and demojized.endswith(":") and text.strip() == text and len(text.strip()) == len(text)
        # Fallback heuristic: basic extended pictographic range (very rough)
        return bool(re.fullmatch(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]", text))


class NotionApi:
    """
    Lightweight wrapper around the official Notion API for optional enrichment.
    """

    def __init__(self, token: str):
        if NotionClient is None:
            raise RuntimeError("notion-client is not installed. Install it or omit --notion-api-token.")
        self.client = NotionClient(auth=token)

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_page_title_icon_times(self, page_id_32hex: str) -> Tuple[Optional[str], Optional[str], Optional[datetime], Optional[datetime]]:
        """
        Attempts to fetch page title, icon (emoji), created_time and last_edited_time
        given a 32-hex ID. The official API expects UUIDs with hyphens; add them.

        Returns: (title, icon_emoji, created_time, last_edited_time)
        Some values may be None depending on API availability.
        """
        # Convert 32-hex into UUID with hyphens: 8-4-4-4-12
        if not re.fullmatch(r"[0-9a-fA-F]{32}", page_id_32hex):
            return (None, None, None, None)
        uuid = f"{page_id_32hex[0:8]}-{page_id_32hex[8:12]}-{page_id_32hex[12:16]}-{page_id_32hex[16:20]}-{page_id_32hex[20:]}"
        try:
            page = self.client.pages.retrieve(page_id=uuid)  # type: ignore
        except Exception:
            return (None, None, None, None)

        title: Optional[str] = None
        icon_emoji: Optional[str] = None
        created_time: Optional[datetime] = None
        last_edited_time: Optional[datetime] = None

        try:
            # Last edited at page level
            le = page.get("last_edited_time")
            if le:
                last_edited_time = datetime.fromisoformat(le.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass

        try:
            # Created time is not always exposed; property-level created_time may exist on databases
            ct = page.get("created_time")
            if ct:
                created_time = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass

        try:
            icon = page.get("icon")
            if icon and icon.get("type") == "emoji":
                icon_emoji = icon.get("emoji")
        except Exception:
            pass

        # Extract title from properties, commonly property named "title" or "Name"
        try:
            props = page.get("properties", {})
            for prop in props.values():
                if prop.get("type") == "title":
                    rich = prop.get("title") or []
                    title_parts = [t.get("plain_text", "") for t in rich]
                    title = "".join(title_parts).strip() or None
                    break
        except Exception:
            pass

        return (title, icon_emoji, created_time, last_edited_time)


class NotionExportRenamer:
    """
    State holder for renames (ID stripping and optional enrichment).
    Collision handling is performed later in a two-pass pipeline.
    """

    def __init__(self, root_path: str, move_md_to_folder: bool = True, notion_api: Optional[NotionApi] = None, source_dirs: Optional[set[str]] = None):
        self.root_path = root_path
        self.move_md_to_folder = move_md_to_folder
        self.notion_api = notion_api
        # path -> RenameResult
        self._rename_cache: Dict[str, RenameResult] = {}
        # For source-aware decisions like md move; contains normalized relative dir paths that exist in input
        self._source_dirs: set[str] = source_dirs or set()

    def _sanitize_name(self, name: str) -> str:
        name = INVALID_FILENAME_CHARS.sub(" ", name).strip()
        if len(name) > 200:
            name = name[:200]
        # Collapse multiple spaces
        name = re.sub(r"\s{2,}", " ", name)
        return name

    def _apply_collision(self, path: str, name_no_ext: str) -> str:
        """
        Deprecated: Collision handling moved to two-pass OutputRegistry.
        This function now acts as identity.
        """
        return name_no_ext

    def _maybe_enrich_from_notion(self, original_name_no_ext: str) -> Tuple[Optional[str], Optional[datetime], Optional[datetime], Optional[str]]:
        """
        Returns: (title_or_none, created_time, last_edited_time, emoji_prefix)
        """
        if self.notion_api is None:
            return (None, None, None, None)
        m = ID_SUFFIX_RE.search(original_name_no_ext)
        if not m:
            return (None, None, None, None)
        page_id = m.group(2)
        title, icon_emoji, created_time, last_edited_time = self.notion_api.get_page_title_icon_times(page_id)
        emoji_prefix = icon_emoji if icon_emoji and EmojiHelper.is_single_emoji(icon_emoji) else None
        if title:
            title = self._sanitize_name(title)
        return (title, created_time, last_edited_time, emoji_prefix)

    def _rewrite_single_basename(self, path_to_rename: str) -> RenameResult:
        """
        Rewrites just the basename of a file/dir. Returns RenameResult.
        """
        if path_to_rename in self._rename_cache:
            return self._rename_cache[path_to_rename]

        path, name = os.path.split(path_to_rename)
        name_no_ext, ext = os.path.splitext(name)

        new_name_no_ext: Optional[str] = None
        created_time: Optional[datetime] = None
        last_edited_time: Optional[datetime] = None
        emoji_prefix: Optional[str] = None

        # If name ends with ID, consider enrichment
        id_match = ID_SUFFIX_RE.search(name_no_ext)
        if id_match:
            # Offline default: strip the ID
            base_part = id_match.group(1)
            new_name_no_ext = base_part

            # Optional enrichment from Notion
            title, ctime, mtime, emoji_pfx = self._maybe_enrich_from_notion(name_no_ext)
            if title:
                # Replace with true title (undo truncation)
                new_name_no_ext = title
            if ctime:
                created_time = ctime
            if mtime:
                last_edited_time = mtime
            if emoji_pfx:
                new_name_no_ext = f"{emoji_pfx} {new_name_no_ext}"
        else:
            # No ID, keep original
            new_name_no_ext = name_no_ext

        # Move .md into same-named folder as !index if a folder with same basename exists in input tree
        if ext.lower() == ".md" and self.move_md_to_folder:
            # Determine rel dir path normalized to forward slashes relative to root
            rel_parent_norm = re.sub(r"[\\]+", "/", path).strip("./")
            # Candidate directory under the same parent with same base name
            candidate_dir_rel = "/".join([p for p in [rel_parent_norm, name_no_ext] if p])
            if candidate_dir_rel in self._source_dirs:
                base_no_ext = new_name_no_ext or ""
                new_name_no_ext = os.path.join(base_no_ext, "!index")

        # Sanitize and collisions
        new_name_no_ext = self._sanitize_name(new_name_no_ext or "")
        new_name_no_ext = self._apply_collision(path, new_name_no_ext)

        result = RenameResult(new_name=f"{new_name_no_ext}{ext}", created_time=created_time, last_edited_time=last_edited_time)
        self._rename_cache[path_to_rename] = result
        return result

    def rename_with_notion(self, path_to_rename: str) -> str:
        return self._rewrite_single_basename(path_to_rename).new_name

    def rename_and_times_with_notion(self, path_to_rename: str) -> RenameResult:
        return self._rewrite_single_basename(path_to_rename)

    def rename_path_with_notion(self, path_to_rename: str) -> str:
        parts = re.split(r"[\\/]", path_to_rename)
        paths = [os.path.join(*parts[0:rpc + 1]) for rpc in range(len(parts))]
        renamed_parts = [self.rename_with_notion(p) for p in paths]
        return os.path.join(*renamed_parts)

    def rename_path_and_times_with_notion(self, path_to_rename: str) -> Tuple[str, Optional[datetime], Optional[datetime]]:
        new_dir = self.rename_path_with_notion(os.path.dirname(path_to_rename))
        r = self.rename_and_times_with_notion(path_to_rename)
        return os.path.join(new_dir, r.new_name), r.created_time, r.last_edited_time


def md_file_rewrite(
    renamer: NotionExportRenamer,
    md_file_path: str,
    md_file_contents: str,
    remove_top_h1: bool = False,
    rewrite_paths: bool = False,
) -> str:
    """
    Rewrites parts of a Notion-exported markdown file.

    Args:
        renamer: NotionExportRenamer instance.
        md_file_path: Original path (relative to root) for the md file.
        md_file_contents: Contents of the Markdown file (UTF-8).
        remove_top_h1: Remove leading H1 line.
        rewrite_paths: Rewrite local links/images to match renamed paths.

    Returns:
        New markdown contents as str.
    """
    new_md = md_file_contents

    if remove_top_h1:
        lines = new_md.split("\n")
        if lines:
            new_md = "\n".join(lines[1:])

    if rewrite_paths:
        search_start = 0
        while True:
            m = MD_LINK_OR_IMAGE_RE.search(new_md, pos=search_start)
            if not m:
                break

            url = m.group(1)
            # Skip absolute or protocol URLs
            if re.search(r":/", url):
                search_start = m.end(1)
                continue
            rel_target = urllib.parse.unquote(url)

            md_dir = os.path.dirname(md_file_path)
            new_target_abs = renamer.rename_path_with_notion(os.path.join(md_dir, rel_target))
            new_md_dir_abs = os.path.dirname(renamer.rename_path_with_notion(md_file_path))
            new_rel = os.path.relpath(new_target_abs, new_md_dir_abs)
            new_rel = re.sub(r"\\", "/", new_rel)
            new_rel = urllib.parse.quote(new_rel)

            new_md = new_md[: m.start(1)] + new_rel + new_md[m.end(1):]
            search_start = m.start(1) + len(new_rel)

    return new_md


def _zipinfo_with_dt(path_in_zip: str, dt: Optional[datetime]) -> zipfile.ZipInfo:
    """
    Create a ZipInfo with a given datetime (fallback to now if None).
    Notion zip requires naive localtime tuple; we approximate by using UTC naive or local time.
    """
    if dt is None:
        dt_use = datetime.now()
    else:
        dt_use = dt.astimezone(timezone.utc).replace(tzinfo=None)
    # ZipInfo expects a 6-tuple (Y, M, D, h, m, s)
    tt = dt_use.timetuple()
    date_tuple = (tt.tm_year, tt.tm_mon, tt.tm_mday, tt.tm_hour, tt.tm_min, tt.tm_sec)
    zi = zipfile.ZipInfo(path_in_zip, date_tuple)
    return zi


def _collect_all_paths(root_dir: str) -> Iterable[Tuple[str, str]]:
    """
    Yields (rel_path, abs_path) for all files under root_dir.
    """
    for tmp_walk_dir, _dirs, files in os.walk(root_dir):
        walk_rel = os.path.relpath(tmp_walk_dir, root_dir)
        for name in files:
            abs_path = os.path.join(tmp_walk_dir, name)
            rel_path = os.path.join("" if walk_rel == "." else walk_rel, name)
            yield rel_path, abs_path


def _build_source_dirs_from_fs(root_dir: str) -> set[str]:
    dirs: set[str] = set()
    for tmp_walk_dir, dirnames, _files in os.walk(root_dir):
        walk_rel = os.path.relpath(tmp_walk_dir, root_dir)
        rel_dir = "" if walk_rel == "." else walk_rel
        norm = re.sub(r"[\\]+", "/", rel_dir).strip("./")
        if norm:
            dirs.add(norm)
        for d in dirnames:
            child = "/".join([p for p in [norm, d] if p])
            dirs.add(child)
    return dirs


def _two_pass_tree_to_zip(
    root_dir: str,
    out_zip_path: str,
    remove_top_h1: bool,
    rewrite_paths: bool,
    renamer: NotionExportRenamer,
) -> None:
    # First pass: collect entries and proposed renamed paths without collision suffixes
    entries: list[Tuple[str, str, bool]] = []  # (rel_path, abs_path, is_md)
    for rel_path, abs_path in _collect_all_paths(root_dir):
        entries.append((rel_path, abs_path, rel_path.lower().endswith(".md")))

    # Resolve proposed renamed paths
    proposed: Dict[str, Tuple[str, Optional[datetime]]] = {}  # rel_path -> (proposed_new_path, mtime)
    for rel_path, _abs_path, _is_md in entries:
        new_path, _created_time, last_edited_time = renamer.rename_path_and_times_with_notion(rel_path)
        proposed[rel_path] = (new_path, last_edited_time)

    # Collision registry per parent directory for zip (in-memory only)
    registry: Dict[str, set[str]] = {}

    def reserve(parent: str, filename: str) -> str:
        parent_norm = _normalize_zip_path(parent).rstrip("/")
        used = registry.setdefault(parent_norm, set())
        if filename not in used:
            used.add(filename)
            return filename
        name_no_ext, ext = os.path.splitext(filename)
        i = 1
        while True:
            cand = f"{name_no_ext} ({i}){ext}"
            if cand not in used:
                used.add(cand)
                return cand
            i += 1

    # Second pass: compute final paths with collision resolution
    final_map: Dict[str, Tuple[str, Optional[datetime]]] = {}
    for rel_path, (_new_path, mtime) in proposed.items():
        parent, fname = os.path.split(_new_path)
        final_fname = reserve(parent, fname)
        final_map[rel_path] = (os.path.join(parent, final_fname), mtime)

    # Now write with path rewriting using final mapping
    def map_path(rel_path_in_md: str, md_file_rel: str) -> str:
        # Resolve target's final absolute-style path in the zip namespace
        target_abs_rel = os.path.normpath(os.path.join(os.path.dirname(md_file_rel), rel_path_in_md))
        target_abs_rel_norm = _normalize_zip_path(target_abs_rel)
        if target_abs_rel_norm in final_map:
            target_final = _normalize_zip_path(final_map[target_abs_rel_norm][0])
        else:
            proposed_target, _ct, _mt = renamer.rename_path_and_times_with_notion(target_abs_rel_norm)
            parent, fname = os.path.split(proposed_target)
            final_fname = reserve(parent, fname)
            target_final = _normalize_zip_path(os.path.join(parent, final_fname))
        # Compute relative path from the md file's final directory
        md_final_path = _normalize_zip_path(final_map[md_file_rel][0])
        md_final_dir = os.path.dirname(md_final_path)
        rel = os.path.relpath(target_final, md_final_dir or ".")
        rel = re.sub(r"[\\]+", "/", rel)
        return rel

    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_path, abs_path, is_md in entries:
            new_path, mtime = final_map[rel_path]
            if is_md:
                with open(abs_path, "r", encoding="utf-8") as f:
                    md_data = f.read()
                # Perform markdown rewrite with final path mapping
                if remove_top_h1 or rewrite_paths:
                    # Temporary renamer proxy for link mapping using map_path
                    def rewrite(md_text: str) -> str:
                        new_md = md_text
                        if remove_top_h1:
                            lines = new_md.split("\n")
                            if lines:
                                new_md = "\n".join(lines[1:])
                        if rewrite_paths:
                            search_start = 0
                            while True:
                                m = MD_LINK_OR_IMAGE_RE.search(new_md, pos=search_start)
                                if not m:
                                    break
                                url = m.group(1)
                                if re.search(r":/", url):
                                    search_start = m.end(1)
                                    continue
                                rel_target = urllib.parse.unquote(url)
                                new_rel = map_path(rel_target, rel_path)
                                new_rel = re.sub(r"\\", "/", new_rel)
                                new_rel = urllib.parse.quote(new_rel)
                                new_md = new_md[: m.start(1)] + new_rel + new_md[m.end(1):]
                                search_start = m.start(1) + len(new_rel)
                        return new_md
                    md_data = rewrite(md_data)
                zi = _zipinfo_with_dt(_normalize_zip_path(new_path), mtime)
                _ensure_zip_parent_dirs(zf, new_path, zi.date_time)
                zf.writestr(zi, md_data.encode("utf-8"))
            elif rel_path.lower().endswith(".csv") and rewrite_paths:
                try:
                    with open(abs_path, "rb") as f:
                        data = f.read()
                    try:
                        csv_text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        # Fallback: copy raw if not UTF-8
                        zi = zipfile.ZipInfo(_normalize_zip_path(new_path), (datetime.now().timetuple()[:6]))
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        _ensure_zip_parent_dirs(zf, new_path, zi.date_time)
                        zf.write(abs_path, _normalize_zip_path(new_path))
                        continue
                    # Conservative regex for relative notion paths (must contain '/'; no scheme)
                    CSV_REL_PATH_RE = re.compile(r"(?P<p>(?!(?:[a-zA-Z][a-zA-Z0-9+.\-]*):)(?:[\w\-._~%]+/)+[\w\-._~%]+(?:\.[A-Za-z0-9]+)?)")
                    search_start = 0
                    out = csv_text
                    while True:
                        m = CSV_REL_PATH_RE.search(out, pos=search_start)
                        if not m:
                            break
                        val = m.group("p")
                        # Skip obvious absolute windows paths
                        if re.match(r"^[A-Za-z]:\\", val):
                            search_start = m.end("p")
                            continue
                        rel_target = urllib.parse.unquote(val)
                        new_rel = map_path(rel_target, rel_path)
                        new_rel = re.sub(r"\\", "/", new_rel)
                        new_rel = urllib.parse.quote(new_rel)
                        out = out[: m.start("p")] + new_rel + out[m.end("p"):]
                        search_start = m.start("p") + len(new_rel)
                    zi = _zipinfo_with_dt(_normalize_zip_path(new_path), mtime)
                    _ensure_zip_parent_dirs(zf, new_path, zi.date_time)
                    zf.writestr(zi, out.encode("utf-8"))
                except Exception:
                    # Fallback to raw copy on error
                    zi = zipfile.ZipInfo(_normalize_zip_path(new_path), (datetime.now().timetuple()[:6]))
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    _ensure_zip_parent_dirs(zf, new_path, zi.date_time)
                    zf.write(abs_path, _normalize_zip_path(new_path))
            else:
                zi = zipfile.ZipInfo(_normalize_zip_path(new_path), (datetime.now().timetuple()[:6]))
                zi.compress_type = zipfile.ZIP_DEFLATED
                _ensure_zip_parent_dirs(zf, new_path, zi.date_time)
                zf.write(abs_path, _normalize_zip_path(new_path))


def _ensure_parent_dir(path: str) -> None:
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)


def _write_file_with_times(dst_path: str, data: bytes, mtime: Optional[datetime]) -> None:
    _ensure_parent_dir(dst_path)
    with open(dst_path, "wb") as f:
        f.write(data)
    # set times if available
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(dst_path, (ts, ts))


def _copy_file_with_times(src_path: str, dst_path: str, mtime: Optional[datetime]) -> None:
    _ensure_parent_dir(dst_path)
    shutil.copy2(src_path, dst_path)
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(dst_path, (ts, ts))


def _two_pass_tree_to_dir(
    root_dir: str,
    dest_dir: str,
    remove_top_h1: bool,
    rewrite_paths: bool,
    renamer: NotionExportRenamer,
) -> None:
    # First pass: collect entries
    entries: list[Tuple[str, str, bool]] = []
    for rel_path, abs_path in _collect_all_paths(root_dir):
        entries.append((rel_path, abs_path, rel_path.lower().endswith(".md")))

    # Proposed mapping
    proposed: Dict[str, Tuple[str, Optional[datetime]]] = {}
    for rel_path, _abs_path, _is_md in entries:
        new_path, _ct, mt = renamer.rename_path_and_times_with_notion(rel_path)
        proposed[rel_path] = (new_path, mt)

    # Registry per output parent, consult existing filesystem under dest_dir
    registry: Dict[str, set[str]] = {}

    def reserve(parent: str, filename: str) -> str:
        parent_norm = re.sub(r"[\\]+", "/", parent).strip("./")
        used = registry.setdefault(parent_norm, set())
        # Ensure parent directory exists for existence checks
        parent_fs = os.path.join(dest_dir, parent_norm)
        Path(parent_fs).mkdir(parents=True, exist_ok=True)
        # Try original
        if filename not in used and not os.path.exists(os.path.join(parent_fs, filename)):
            used.add(filename)
            return filename
        name_no_ext, ext = os.path.splitext(filename)
        i = 1
        while True:
            cand = f"{name_no_ext} ({i}){ext}"
            if cand not in used and not os.path.exists(os.path.join(parent_fs, cand)):
                used.add(cand)
                return cand
            i += 1

    # Final mapping with collisions handled
    final_map: Dict[str, Tuple[str, Optional[datetime]]] = {}
    for rel_path, (p_new, mt) in proposed.items():
        parent, fname = os.path.split(p_new)
        final_fname = reserve(parent, fname)
        final_map[rel_path] = (os.path.join(parent, final_fname), mt)

    # Helper for markdown link path mapping
    def map_path(rel_path_in_md: str, md_file_rel: str) -> str:
        target_abs_rel = os.path.normpath(os.path.join(os.path.dirname(md_file_rel), rel_path_in_md))
        target_abs_rel_norm = re.sub(r"[\\]+", "/", target_abs_rel).strip("./")
        if target_abs_rel_norm in final_map:
            target_final = re.sub(r"[\\]+", "/", final_map[target_abs_rel_norm][0])
        else:
            proposed_target, _ct, _mt = renamer.rename_path_and_times_with_notion(target_abs_rel_norm)
            parent, fname = os.path.split(proposed_target)
            final_fname = reserve(parent, fname)
            target_final = re.sub(r"[\\]+", "/", os.path.join(parent, final_fname))
        md_final_path = re.sub(r"[\\]+", "/", final_map[md_file_rel][0])
        md_final_dir = os.path.dirname(md_final_path)
        rel = os.path.relpath(target_final, md_final_dir or ".")
        rel = re.sub(r"[\\]+", "/", rel)
        return rel

    # Write out
    for rel_path, abs_path, is_md in entries:
        final_rel, mtime = final_map[rel_path]
        final_dst = os.path.join(dest_dir, final_rel)
        print("---")
        print(f"Writing to dest for '{rel_path}' -> '{final_rel}'")
        if is_md:
            with open(abs_path, "r", encoding="utf-8") as f:
                md_data = f.read()
            # Rewrite with final mapping
            if remove_top_h1 or rewrite_paths:
                def rewrite(md_text: str) -> str:
                    new_md = md_text
                    if remove_top_h1:
                        lines = new_md.split("\n")
                        if lines:
                            new_md = "\n".join(lines[1:])
                    if rewrite_paths:
                        search_start = 0
                        while True:
                            m = MD_LINK_OR_IMAGE_RE.search(new_md, pos=search_start)
                            if not m:
                                break
                            url = m.group(1)
                            if re.search(r":/", url):
                                search_start = m.end(1)
                                continue
                            rel_target = urllib.parse.unquote(url)
                            new_rel = map_path(rel_target, rel_path)
                            new_rel = re.sub(r"\\", "/", new_rel)
                            new_rel = urllib.parse.quote(new_rel)
                            new_md = new_md[: m.start(1)] + new_rel + new_md[m.end(1):]
                            search_start = m.start(1) + len(new_rel)
                    return new_md
                md_data = rewrite(md_data)
            print(f"Writing file '{final_dst}' with time '{mtime}'")
            _write_file_with_times(final_dst, md_data.encode("utf-8"), mtime)
        elif rel_path.lower().endswith(".csv") and rewrite_paths:
            try:
                with open(abs_path, "rb") as f:
                    data = f.read()
                try:
                    csv_text = data.decode("utf-8")
                except UnicodeDecodeError:
                    print(f"Copying file to '{final_dst}'")
                    _copy_file_with_times(abs_path, final_dst, None)
                    continue
                CSV_REL_PATH_RE = re.compile(r"(?P<p>(?!(?:[a-zA-Z][a-zA-Z0-9+.\-]*):)(?:[\w\-._~%]+/)+[\w\-._~%]+(?:\.[A-Za-z0-9]+)?)")
                search_start = 0
                out = csv_text
                while True:
                    m = CSV_REL_PATH_RE.search(out, pos=search_start)
                    if not m:
                        break
                    val = m.group("p")
                    if re.match(r"^[A-Za-z]:\\", val):
                        search_start = m.end("p")
                        continue
                    rel_target = urllib.parse.unquote(val)
                    new_rel = map_path(rel_target, rel_path)
                    new_rel = re.sub(r"\\", "/", new_rel)
                    new_rel = urllib.parse.quote(new_rel)
                    out = out[: m.start("p")] + new_rel + out[m.end("p"):]
                    search_start = m.start("p") + len(new_rel)
                print(f"Writing file '{final_dst}' with time '{mtime}'")
                _write_file_with_times(final_dst, out.encode("utf-8"), mtime)
            except Exception:
                print(f"Copying file to '{final_dst}'")
                _copy_file_with_times(abs_path, final_dst, None)
        else:
            print(f"Copying file to '{final_dst}'")
            _copy_file_with_times(abs_path, final_dst, None)


def _safe_delete_dir_contents(path: str) -> None:
    p = Path(path)
    if p.exists():
        if not p.is_dir():
            raise NotADirectoryError(f"Destination path exists but is not a directory: {path}")
        for child in p.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    else:
        p.mkdir(parents=True, exist_ok=True)


def _is_zip(path: str) -> bool:
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with zipfile.ZipFile(str(p)) as _:
            return True
    except zipfile.BadZipFile:
        return False


def _normalize_zip_path(p: str) -> str:
    # Zip files use forward slashes
    return re.sub(r"[\\]+", "/", p).lstrip("./")


def _emit_dir_entry(zf: zipfile.ZipFile, dir_path: str, date_time: Tuple[int, int, int, int, int, int]) -> None:
    dp = _normalize_zip_path(dir_path).rstrip("/") + "/"
    if dp == "/":
        return
    zi = zipfile.ZipInfo(dp, date_time)
    zf.writestr(zi, b"")


def _ensure_zip_parent_dirs(zf: zipfile.ZipFile, file_path: str, date_time: Tuple[int, int, int, int, int, int]) -> None:
    # Emit all parent directory entries for a given file path
    norm = _normalize_zip_path(file_path)
    parts = norm.split("/")
    acc = ""
    for i in range(len(parts) - 1):
        acc = f"{acc}{parts[i]}/"
        _emit_dir_entry(zf, acc, date_time)


def _two_pass_zip_to_zip(
    inner_zip: zipfile.ZipFile,
    out_zip_path: str,
    remove_top_h1: bool,
    rewrite_paths: bool,
    renamer: NotionExportRenamer,
    strip_top_folder: bool = False,
) -> None:
    # Build list of file entries (skip directories; we will emit needed dirs)
    file_infos: list[zipfile.ZipInfo] = []
    for info in inner_zip.infolist():
        if info.is_dir():
            continue
        file_infos.append(info)

    # Detect single top-level common folder if stripping
    top_common: Optional[str] = None
    if strip_top_folder:
        names = [info.filename for info in inner_zip.infolist() if info.filename]
        norm_names = [_normalize_zip_path(n) for n in names]
        top_levels = {n.split("/")[0] for n in norm_names if "/" in n}
        if len(top_levels) == 1:
            t = next(iter(top_levels))
            if all(n == t or n.startswith(t + "/") for n in norm_names):
                top_common = t

    # Build proposed mapping
    proposed: Dict[str, Tuple[str, Optional[datetime], Tuple[int, int, int, int, int, int]]] = {}  # eff_rel -> (proposed_path, mtime, date_time)
    for info in file_infos:
        effective_rel = _normalize_zip_path(info.filename)
        if strip_top_folder and top_common:
            if effective_rel == top_common or effective_rel.startswith(top_common + "/"):
                effective_rel = effective_rel[len(top_common) + 1 :] if effective_rel != top_common else ""
                if not effective_rel:
                    continue
        new_path, _ct, mt = renamer.rename_path_and_times_with_notion(effective_rel)
        proposed[effective_rel] = (_normalize_zip_path(new_path), mt, info.date_time)

    # Registry for collisions
    registry: Dict[str, set[str]] = {}

    def reserve(parent: str, filename: str) -> str:
        parent_norm = _normalize_zip_path(parent).rstrip("/")
        used = registry.setdefault(parent_norm, set())
        if filename not in used:
            used.add(filename)
            return filename
        name_no_ext, ext = os.path.splitext(filename)
        i = 1
        while True:
            cand = f"{name_no_ext} ({i}){ext}"
            if cand not in used:
                used.add(cand)
                return cand
            i += 1

    # Final mapping
    final_map: Dict[str, Tuple[str, Optional[datetime], Tuple[int, int, int, int, int, int]]] = {}
    for eff_rel, (p_new, mt, dt) in proposed.items():
        parent, fname = os.path.split(p_new)
        final_fname = reserve(parent, fname)
        final_map[eff_rel] = (os.path.join(parent, final_fname), mt, dt)

    # Helper for link path mapping
    def map_path(rel_path_in_md: str, md_file_rel: str) -> str:
        target_abs_rel = os.path.normpath(os.path.join(os.path.dirname(md_file_rel), rel_path_in_md))
        target_abs_rel_norm = _normalize_zip_path(target_abs_rel)
        if target_abs_rel_norm in final_map:
            target_final = _normalize_zip_path(final_map[target_abs_rel_norm][0])
        else:
            proposed_target, _ct, _mt = renamer.rename_path_and_times_with_notion(target_abs_rel_norm)
            parent, fname = os.path.split(proposed_target)
            final_fname = reserve(parent, fname)
            target_final = _normalize_zip_path(os.path.join(parent, final_fname))
        md_final_path = _normalize_zip_path(final_map[md_file_rel][0])
        md_final_dir = os.path.dirname(md_final_path)
        rel = os.path.relpath(target_final, md_final_dir or ".")
        rel = re.sub(r"[\\]+", "/", rel)
        return rel

    failed_write_entries = []

    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as outzf:
        # Emit directories as needed when writing files
        for info in file_infos:
            effective_rel = _normalize_zip_path(info.filename)
            if strip_top_folder and top_common:
                if effective_rel == top_common or effective_rel.startswith(top_common + "/"):
                    effective_rel = effective_rel[len(top_common) + 1 :] if effective_rel != top_common else ""
                    if not effective_rel:
                        continue
            data = inner_zip.read(info)
            if effective_rel.lower().endswith(".md"):
                try:
                    try:
                        md_text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        print(f"Warning: '{effective_rel}' not UTF-8; copying without markdown rewrite.")
                        final_path, mt, dt = final_map[effective_rel]
                        zi = _zipinfo_with_dt(final_path, mt)
                        _ensure_zip_parent_dirs(outzf, final_path, dt)
                        outzf.writestr(zi, data)
                        continue
                    # Rewrite with final mapping
                    if remove_top_h1 or rewrite_paths:
                        def rewrite(md_text_in: str) -> str:
                            new_md = md_text_in
                            if remove_top_h1:
                                lines = new_md.split("\n")
                                if lines:
                                    new_md = "\n".join(lines[1:])
                            if rewrite_paths:
                                search_start = 0
                                while True:
                                    m = MD_LINK_OR_IMAGE_RE.search(new_md, pos=search_start)
                                    if not m:
                                        break
                                    url = m.group(1)
                                    if re.search(r":/", url):
                                        search_start = m.end(1)
                                        continue
                                    rel_target = urllib.parse.unquote(url)
                                    new_rel = map_path(rel_target, effective_rel)
                                    new_rel = re.sub(r"\\", "/", new_rel)
                                    new_rel = urllib.parse.quote(new_rel)
                                    new_md = new_md[: m.start(1)] + new_rel + new_md[m.end(1):]
                                    search_start = m.start(1) + len(new_rel)
                            return new_md
                        md_text = rewrite(md_text)
                    final_path, mt, dt = final_map[effective_rel]
                    zi = _zipinfo_with_dt(final_path, mt)
                    _ensure_zip_parent_dirs(outzf, final_path, dt)
                    outzf.writestr(zi, md_text.encode("utf-8"))
                except Exception as e:
                    final_path, _mt, _dt = final_map.get(effective_rel, ("<unknown>", None, info.date_time))
                    print(f"[WARN] Failed to write markdown entry '{effective_rel}' as '{final_path}': {e}")
                    failed_write_entries.append((effective_rel, final_path, str(e)))
            elif effective_rel.lower().endswith(".csv") and rewrite_paths:
                try:
                    try:
                        csv_text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        final_path, _mt, dt = final_map[effective_rel]
                        zi = zipfile.ZipInfo(_normalize_zip_path(final_path), dt)
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        _ensure_zip_parent_dirs(outzf, final_path, dt)
                        outzf.writestr(zi, data)
                        continue
                    CSV_REL_PATH_RE = re.compile(r"(?P<p>(?!(?:[a-zA-Z][a-zA-Z0-9+.\-]*):)(?:[\w\-._~%]+/)+[\w\-._~%]+(?:\.[A-Za-z0-9]+)?)")
                    search_start = 0
                    out_text = csv_text
                    while True:
                        m = CSV_REL_PATH_RE.search(out_text, pos=search_start)
                        if not m:
                            break
                        val = m.group("p")
                        if re.match(r"^[A-Za-z]:\\", val):
                            search_start = m.end("p")
                            continue
                        rel_target = urllib.parse.unquote(val)
                        new_rel = map_path(rel_target, effective_rel)
                        new_rel = re.sub(r"\\", "/", new_rel)
                        new_rel = urllib.parse.quote(new_rel)
                        out_text = out_text[: m.start("p")] + new_rel + out_text[m.end("p"):]
                        search_start = m.start("p") + len(new_rel)
                    final_path, mt, dt = final_map[effective_rel]
                    zi = _zipinfo_with_dt(final_path, mt)
                    _ensure_zip_parent_dirs(outzf, final_path, dt)
                    outzf.writestr(zi, out_text.encode("utf-8"))
                except Exception as e:
                    final_path, _mt, _dt = final_map.get(effective_rel, ("<unknown>", None, info.date_time))
                    print(f"[WARN] Failed to write csv entry '{effective_rel}' as '{final_path}': {e}")
                    failed_write_entries.append((effective_rel, final_path, str(e)))
            else:
                try:
                    final_path, _mt, dt = final_map[effective_rel]
                    zi = zipfile.ZipInfo(_normalize_zip_path(final_path), dt)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    _ensure_zip_parent_dirs(outzf, final_path, dt)
                    outzf.writestr(zi, data)
                except Exception as e:
                    final_path, _mt, _dt = final_map.get(effective_rel, ("<unknown>", None, info.date_time))
                    print(f"[WARN] Failed to write entry '{effective_rel}' as '{final_path}': {e}")
                    failed_write_entries.append((effective_rel, final_path, str(e)))


def _extract_zip_to_dir(zip_path: str, dest_dir: str) -> None:
    """
    Extracts a zip file into dest_dir.
    Any file that fails to extract is logged and skipped; a summary is printed at the end.
    """
    failures = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            # Normalize to forward slashes (zip standard) then build OS path
            name = _normalize_zip_path(info.filename)
            target_path = os.path.normpath(os.path.join(dest_dir, name))
            try:
                if name.endswith("/"):
                    # Explicit directory entry
                    Path(target_path).mkdir(parents=True, exist_ok=True)
                    continue
                parent_dir = Path(target_path).parent
                parent_dir.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except Exception as e:
                print(f"[WARN] Failed to extract '{name}' -> '{target_path}': {e}")
                failures.append((name, str(e)))
    if failures:
        print(f"[SUMMARY] Extraction completed with {len(failures)} failure(s). Listing failed entries:")
        for idx, (n, err) in enumerate(failures, 1):
            print(f"  {idx}. '{n}': {err}")
    else:
        print("[SUMMARY] Extraction completed with 0 failures.")


def _print_fail_summary(context: str, failures: Iterable[Tuple[str, ...]]) -> None:
    """
    Print a uniform summary for failures captured during processing.
    """
    failures = list(failures)
    if not failures:
        return
    print(f"[SUMMARY] {context}: {len(failures)} failure(s):")
    for i, f in enumerate(failures, 1):
        print(f"  {i}. " + " | ".join(f))


def rewrite_notion_export(
    zip_or_dir_path: str,
    output_path: str = ".",
    remove_top_h1: bool = False,
    rewrite_paths: bool = True,
    move_md_to_folder: bool = True,
    notion_api_token: Optional[str] = None,
    dest_dir: Optional[str] = None,
) -> str:
    """
    Enhances a Notion export from zip or directory and writes a formatted zip.

    Args:
        zip_or_dir_path: Path to a Notion zip or a directory.
        output_path: Output directory to place the resulting zip.
        remove_top_h1: Remove first H1 in md files.
        rewrite_paths: Rewrite links/images within md files.
        move_md_to_folder: Move root md into folder as !index.md when a same-named folder exists.
        notion_api_token: Optional Notion integration token to enrich names/icons/timestamps.

    Returns:
        Path to the output zip file.
    """
    output_path = str(Path(output_path))
    Path(output_path).mkdir(parents=True, exist_ok=True)

    notion_api: Optional[NotionApi] = None
    if notion_api_token:
        notion_api = NotionApi(notion_api_token)

    if _is_zip(zip_or_dir_path):
        with tempfile.TemporaryDirectory() as tmp_dir:
            print(f"Extracting '{zip_or_dir_path}' temporarily...")
            with zipfile.ZipFile(zip_or_dir_path) as outer_zf:
                outer_zf.extractall(tmp_dir)

            # Detect a single wrapper zip at the top level
            top_entries = [p for p in Path(tmp_dir).iterdir()]
            wrapper_zips = [str(p) for p in top_entries if p.is_file() and p.suffix.lower() == ".zip"]

            zip_name = os.path.basename(zip_or_dir_path)
            new_zip_name = f"{zip_name}.formatted"
            new_zip_path = os.path.join(output_path, new_zip_name)

            if len(wrapper_zips) == 1:
                wrapper_path = wrapper_zips[0]
                print(f"Detected inner wrapper zip: '{wrapper_path}'")
                # Build source dirs from inner zip to support md move rule
                source_dirs: set[str] = set()
                with zipfile.ZipFile(wrapper_path, "r") as wz:
                    for info in wz.infolist():
                        if info.is_dir():
                            name = _normalize_zip_path(info.filename).rstrip("/")
                            if name:
                                source_dirs.add(name)
                        else:
                            # include parent dirs of files
                            name = _normalize_zip_path(info.filename)
                            parent = os.path.dirname(name)
                            if parent:
                                source_dirs.add(parent)
                # Build renamer against virtual root (paths in the inner zip)
                renamer = NotionExportRenamer("/", move_md_to_folder=move_md_to_folder, notion_api=notion_api, source_dirs=source_dirs)
                # Transform inner zip -> formatted zip without extracting it; strip top Export-xxx folder for dest_dir flattening
                _two_pass_zip_to_zip(
                    zipfile.ZipFile(wrapper_path, "r"),
                    new_zip_path,
                    remove_top_h1=remove_top_h1,
                    rewrite_paths=rewrite_paths,
                    renamer=renamer,
                    strip_top_folder=True,
                )
                # Note: _process_zip_to_zip already logs any failed write entries.
                # If destination directory is provided, clean then extract newly produced formatted zip into it
                if dest_dir:
                    dest_dir_abs = str(Path(dest_dir))
                    if os.path.abspath(dest_dir_abs) == os.path.abspath(tmp_dir):
                        raise RuntimeError("Destination directory must not be the temporary extraction directory.")
                    print(f"Cleaning destination directory '{dest_dir_abs}' ...")
                    _safe_delete_dir_contents(dest_dir_abs)
                    print(f"Extracting formatted zip into destination directory '{dest_dir_abs}' ...")
                    _extract_zip_to_dir(new_zip_path, dest_dir_abs)
                    # Remove the intermediate formatted zip as it is not desired to persist
                    try:
                        if os.path.exists(new_zip_path):
                            os.remove(new_zip_path)
                            print(f"[CLEANUP] Removed intermediate archive '{new_zip_path}'")
                    except Exception as e:
                        print(f"[WARN] Failed to remove intermediate archive '{new_zip_path}': {e}")
                return new_zip_path
            else:
                # Fallback to existing directory-based processing from the extracted outer zip
                source_dirs = _build_source_dirs_from_fs(tmp_dir)
                renamer = NotionExportRenamer(tmp_dir, move_md_to_folder=move_md_to_folder, notion_api=notion_api, source_dirs=source_dirs)

                if dest_dir:
                    dest_dir_abs = str(Path(dest_dir))
                    if os.path.abspath(dest_dir_abs) == os.path.abspath(tmp_dir):
                        raise RuntimeError("Destination directory must not be the temporary extraction directory.")
                    print(f"Cleaning destination directory '{dest_dir_abs}' ...")
                    _safe_delete_dir_contents(dest_dir_abs)
                    print(f"Processing tree into destination directory '{dest_dir_abs}' ...")
                    _two_pass_tree_to_dir(
                        tmp_dir,
                        dest_dir_abs,
                        remove_top_h1=remove_top_h1,
                        rewrite_paths=rewrite_paths,
                        renamer=renamer,
                    )

                _two_pass_tree_to_zip(
                    tmp_dir,
                    new_zip_path,
                    remove_top_h1=remove_top_h1,
                    rewrite_paths=rewrite_paths,
                    renamer=renamer,
                )
                # If we were asked to materialize a dest_dir, extract and then remove the zip
                if dest_dir:
                    dest_dir_abs = str(Path(dest_dir))
                    print(f"Extracting formatted zip into destination directory '{dest_dir_abs}' ...")
                    _extract_zip_to_dir(new_zip_path, dest_dir_abs)
                    try:
                        if os.path.exists(new_zip_path):
                            os.remove(new_zip_path)
                            print(f"[CLEANUP] Removed intermediate archive '{new_zip_path}'")
                    except Exception as e:
                        print(f"[WARN] Failed to remove intermediate archive '{new_zip_path}': {e}")
                return new_zip_path
    else:
        # Treat as directory
        src_dir = Path(zip_or_dir_path)
        if not src_dir.exists() or not src_dir.is_dir():
            raise FileNotFoundError(f"Path is neither a zip nor a directory: {zip_or_dir_path}")

        dir_name = src_dir.name
        new_zip_name = f"{dir_name}.formatted.zip"
        new_zip_path = os.path.join(output_path, new_zip_name)

        source_dirs = _build_source_dirs_from_fs(str(src_dir))
        renamer = NotionExportRenamer(str(src_dir), move_md_to_folder=move_md_to_folder, notion_api=notion_api, source_dirs=source_dirs)
        _two_pass_tree_to_zip(
            str(src_dir),
            new_zip_path,
            remove_top_h1=remove_top_h1,
            rewrite_paths=rewrite_paths,
            renamer=renamer,
        )
        # If a destination directory is specified for directory inputs, mirror behavior:
        if dest_dir:
            dest_dir_abs = str(Path(dest_dir))
            print(f"Cleaning destination directory '{dest_dir_abs}' ...")
            _safe_delete_dir_contents(dest_dir_abs)
            print(f"Extracting formatted zip into destination directory '{dest_dir_abs}' ...")
            _extract_zip_to_dir(new_zip_path, dest_dir_abs)
            try:
                if os.path.exists(new_zip_path):
                    os.remove(new_zip_path)
                    print(f"[CLEANUP] Removed intermediate archive '{new_zip_path}'")
            except Exception as e:
                print(f"[WARN] Failed to remove intermediate archive '{new_zip_path}': {e}")
        return new_zip_path


def main(argv: Optional[Iterable[str]] = None) -> None:
    """
    CLI entrypoint.
    """
    parser = argparse.ArgumentParser(description="Prettifies Notion exports (zip or directory).")
    parser.add_argument("zip_or_dir_path", type=str, help="Path to Notion exported .zip or extracted directory")
    parser.add_argument("--output-path", type=str, default=".", help="Directory to write the output .zip to (default: current directory)")
    parser.add_argument("--dest-dir", type=str, default=None, help="If input is a zip, write the processed and link-rewritten tree into this destination directory after cleaning its contents")
    parser.add_argument("--remove-title", action="store_true", help="Removes the H1 title at the top of markdown files")
    parser.add_argument("--no-rewrite-paths", action="store_true", help="Disable rewriting of relative paths in Markdown")
    parser.add_argument("--dont-move-md-to-folder", dest="move_md_to_folder", action="store_false", default=True, help="Do not move root md into folder as !index.md")
    parser.add_argument("--notion-api-token", type=str, default=None, help="Optional Notion integration token for enrichment")
    parser.add_argument("--log-level", type=str, default="info", choices=["debug", "info", "warning", "error"])
    args = parser.parse_args(list(argv) if argv is not None else None)

    # Basic logging based on level; using print to keep dependencies minimal
    def log_debug(msg: str) -> None:
        if args.log_level.lower() == "debug":
            print(msg)

    start_time = time.time()
    log_debug("Starting export fix...")

    out_file = rewrite_notion_export(
        args.zip_or_dir_path,
        output_path=args.output_path,
        remove_top_h1=args.remove_title,
        rewrite_paths=not args.no_rewrite_paths,
        move_md_to_folder=args.move_md_to_folder,
        notion_api_token=args.notion_api_token,
        dest_dir=args.dest_dir,
    )
    print(f"--- Finished in {time.time() - start_time:.2f} seconds ---")
    print(f"Output file written as '{out_file}'")


if __name__ == "__main__":
    main()
