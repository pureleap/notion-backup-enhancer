"""Markdown link rewriting."""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Dict

from .naming import NotionExportRenamer
from .ziputils import _normalize_zip_path

MD_LINK_OR_IMAGE_RE = re.compile(r"!?\[.+?\]\(([\w\d\-._~:/?=#%\]\[@!$&'\(\)*+,;]+?)\)")


def md_file_rewrite(
    renamer: NotionExportRenamer,
    md_file_path: str,
    md_file_contents: str,
    final_map: Dict[str, str],
) -> str:
    """
    Rewrites links in a Notion-exported markdown file to match renamed paths.

    Args:
        renamer: NotionExportRenamer instance.
        md_file_path: Original path (relative to root) for the md file.
        md_file_contents: Contents of the Markdown file (UTF-8).
        final_map: Mapping of original paths to final renamed paths.

    Returns:
        New markdown contents as str.
    """
    new_md = md_file_contents
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

        # Resolve target's final path
        md_dir = os.path.dirname(md_file_path)
        target_abs_rel = os.path.normpath(os.path.join(md_dir, rel_target))
        target_abs_rel_norm = _normalize_zip_path(target_abs_rel)
        if target_abs_rel_norm in final_map:
            target_final = _normalize_zip_path(final_map[target_abs_rel_norm])
        else:
            # Fallback: rename the target path
            target_final = _normalize_zip_path(renamer.rename_path(target_abs_rel_norm))

        # Compute relative path from the md file's final directory
        md_final_path = _normalize_zip_path(final_map.get(md_file_path, md_file_path))
        md_final_dir = os.path.dirname(md_final_path)
        new_rel = os.path.relpath(target_final, md_final_dir or ".")
        new_rel = re.sub(r"[\\]+", "/", new_rel)
        new_rel = urllib.parse.quote(new_rel)

        new_md = new_md[: m.start(1)] + new_rel + new_md[m.end(1) :]
        search_start = m.start(1) + len(new_rel)

    return new_md
