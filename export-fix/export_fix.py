#!/usr/bin/env python3
"""
Export Fix - Simple Notion export enhancer

Features:
- Takes a Notion export .zip file as input
- Removes trailing 32-hex Notion IDs from filenames and directories
- Fixes links pointing to renamed files
- Handles naming collisions by appending " (i)"
- Produces a new .zip file named <input>.fixed.zip
- Creates a log file <input>.log.txt with errors, duplicate files, and filename length issues

CLI:
  export_fix.py <zip_path> [--use-disk-extraction]

Dependencies:
- Python 3.13+

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
import urllib.parse
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
import shutil


ID_SUFFIX_RE = re.compile(r"(.+?)\s([0-9a-f]{32})$", re.IGNORECASE)
MD_LINK_OR_IMAGE_RE = re.compile(
    r"!?\[.+?\]\(([\w\d\-._~:/?=#%\]\[@!$&'\(\)*+,;]+?)\)"
)
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


class NotionExportRenamer:
    """
    State holder for renames (ID stripping).
    Collision handling is performed later in a two-pass pipeline.
    """

    def __init__(self, filename_too_long_tracker=None):
        # path -> new_name
        self._rename_cache: Dict[str, str] = {}
        self._filename_too_long_tracker = filename_too_long_tracker

    def _sanitize_name(self, name: str, original_path: str = "") -> str:
        original_name = name
        name = INVALID_FILENAME_CHARS.sub(" ", name).strip()
        if len(name) > 200:
            if self._filename_too_long_tracker is not None:
                self._filename_too_long_tracker.append((original_path, original_name, len(original_name)))
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

        # If name ends with ID, strip it
        id_match = ID_SUFFIX_RE.search(name_no_ext)
        if id_match:
            base_part = id_match.group(1)
            new_name_no_ext = base_part
        else:
            # No ID, keep original
            new_name_no_ext = name_no_ext

        # Sanitize
        new_name_no_ext = self._sanitize_name(new_name_no_ext, path_to_rename)

        result = f"{new_name_no_ext}{ext}"
        self._rename_cache[path_to_rename] = result
        return result

    def rename_path(self, path_to_rename: str) -> str:
        parts = re.split(r"[\\/]", path_to_rename)
        paths = [os.path.join(*parts[0:rpc + 1]) for rpc in range(len(parts))]
        renamed_parts = [self._rewrite_single_basename(p) for p in paths]
        return os.path.join(*renamed_parts)


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

        new_md = new_md[: m.start(1)] + new_rel + new_md[m.end(1):]
        search_start = m.start(1) + len(new_rel)

    return new_md


def _normalize_zip_path(p: str) -> str:
    # Zip files use forward slashes
    return re.sub(r"[\\]+", "/", p).lstrip("./")


def _ensure_zip_parent_dirs(zf: zipfile.ZipFile, file_path: str, date_time: Tuple[int, int, int, int, int, int]) -> None:
    # Emit all parent directory entries for a given file path
    norm = _normalize_zip_path(file_path)
    parts = norm.split("/")
    acc = ""
    for i in range(len(parts) - 1):
        acc = f"{acc}{parts[i]}/"
        zi = zipfile.ZipInfo(acc, date_time)
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


def process_notion_zip(zip_path: str, use_disk_extraction: bool = False) -> str:
    """
    Processes a Notion export zip file by removing IDs from filenames and fixing links.

    Args:
        zip_path: Path to the input Notion export .zip file
        use_disk_extraction: If True, extract to disk before processing (original method).
                           If False (default), process files directly from zip to avoid path length issues.

    Returns:
        Path to the output zip file (placed in same directory as input)
    """
    if not _is_zip(zip_path):
        raise ValueError(f"Input must be a zip file: {zip_path}")

    # Place output in same directory as input file
    input_dir = os.path.dirname(os.path.abspath(zip_path))
    if not input_dir:
        input_dir = "."  # If no directory, use current directory

    zip_name = os.path.basename(zip_path)
    if zip_name.lower().endswith('.zip'):
        base_name = zip_name[:-4]  # Remove .zip extension
    else:
        base_name = zip_name
    new_zip_name = f"{base_name}.fixed.zip"
    new_zip_path = os.path.join(input_dir, new_zip_name)
    log_file_path = os.path.join(input_dir, f"{base_name}.log.txt")

    # Initialize error and duplicate collections
    errors = []
    duplicates = {}
    filename_too_long = []

    print(f"Processing '{zip_path}'...")
    if use_disk_extraction:
        print("Using disk extraction mode (original method)")
    else:
        print("Using zip-to-zip processing mode (resilient to path length issues)")

    if use_disk_extraction:
        # Original disk-based processing
        # Extract to temp directory
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    try:
                        zf.extract(info, tmp_dir)
                    except Exception as e:
                        error_msg = f"Failed to extract '{info.filename}': {e}"
                        print(f"Warning: {error_msg}")
                        errors.append((info.filename, error_msg))
                        continue

            # Handle nested zip (Notion sometimes wraps the export in another zip)
            top_entries = list(Path(tmp_dir).iterdir())
            if len(top_entries) == 1 and top_entries[0].is_file() and top_entries[0].suffix.lower() == ".zip":
                inner_zip_path = str(top_entries[0])
                print(f"Detected nested zip: {inner_zip_path}")
                with zipfile.ZipFile(inner_zip_path) as inner_zf:
                    for info in inner_zf.infolist():
                        try:
                            inner_zf.extract(info, tmp_dir)
                        except Exception as e:
                            error_msg = f"Failed to extract from nested zip '{info.filename}': {e}"
                            print(f"Warning: {error_msg}")
                            errors.append((info.filename, error_msg))
                            continue

            # Initialize renamer
            renamer = NotionExportRenamer(filename_too_long_tracker=filename_too_long)

            # First pass: build rename mapping
            file_entries = []
            try:
                for root, dirs, files in os.walk(tmp_dir):
                    for file in files:
                        # Skip zip files - we only want their extracted contents
                        if file.lower().endswith('.zip'):
                            continue
                        try:
                            abs_path = os.path.join(root, file)
                            rel_path = os.path.relpath(abs_path, tmp_dir)
                            file_entries.append((rel_path, None, abs_path))
                        except (OSError, ValueError) as e:
                            error_msg = f"Skipping file due to path error: {os.path.join(root, file)} - {e}"
                            print(f"Warning: {error_msg}")
                            errors.append((os.path.join(root, file), error_msg))
                            continue
            except Exception as e:
                error_msg = f"Error during file enumeration: {e}"
                print(f"Warning: {error_msg}")
                errors.append(("", error_msg))
                # Continue with what we have collected so far

            # Build proposed renames
            proposed: Dict[str, str] = {}
            for rel_path, _, _ in file_entries:
                new_path = renamer.rename_path(rel_path)
                proposed[rel_path] = new_path

            # Detect duplicates
            proposed_to_originals = defaultdict(list)
            for orig, prop in proposed.items():
                proposed_to_originals[prop].append(orig)
            for prop, origs in proposed_to_originals.items():
                if len(origs) > 1:
                    duplicates[prop] = origs

            # Handle collisions
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

            # Final mapping with collision resolution
            final_map: Dict[str, str] = {}
            for rel_path, proposed_path in proposed.items():
                parent, fname = os.path.split(proposed_path)
                final_fname = reserve(parent, fname)
                final_map[rel_path] = os.path.join(parent, final_fname)

            # Second pass: write output zip with renamed files and fixed links
            with zipfile.ZipFile(new_zip_path, "w", zipfile.ZIP_DEFLATED) as out_zf:
                for rel_path, abs_path in file_entries:
                    final_path = final_map[rel_path]

                    try:
                        if rel_path.lower().endswith(".md"):
                            # Read and rewrite markdown
                            try:
                                with open(abs_path, "r", encoding="utf-8") as f:
                                    md_content = f.read()
                                md_content = md_file_rewrite(renamer, rel_path, md_content, final_map)
                                zi = zipfile.ZipInfo(_normalize_zip_path(final_path))
                                _ensure_zip_parent_dirs(out_zf, final_path, zi.date_time)
                                out_zf.writestr(zi, md_content.encode("utf-8"))
                            except Exception as e:
                                error_msg = f"Failed to process markdown '{rel_path}': {e}"
                                print(f"Warning: {error_msg}")
                                errors.append((rel_path, error_msg))
                                # Copy as-is
                                zi = zipfile.ZipInfo(_normalize_zip_path(final_path))
                                _ensure_zip_parent_dirs(out_zf, final_path, zi.date_time)
                                out_zf.write(abs_path, _normalize_zip_path(final_path))
                        else:
                            # Copy other files as-is
                            zi = zipfile.ZipInfo(_normalize_zip_path(final_path))
                            _ensure_zip_parent_dirs(out_zf, final_path, zi.date_time)
                            out_zf.write(abs_path, _normalize_zip_path(final_path))
                    except Exception as e:
                        error_msg = f"Failed to write file '{rel_path}' to output zip: {e}"
                        print(f"Warning: {error_msg}")
                        errors.append((rel_path, error_msg))
                        continue
    else:
        # New zip-to-zip processing (resilient to path length issues)
        with zipfile.ZipFile(zip_path) as zf:
            # Get all file paths from the main zip
            all_files = [info.filename for info in zf.infolist() if not info.is_dir()]

            # Check for nested zip - extract ONLY the zip file when found
            nested_zip_data = None
            nested_zip_name = None
            nested_files = []

            # Look for any zip file in the main zip
            zip_files = [f for f in all_files if f.lower().endswith('.zip')]
            if zip_files:
                # Use the first zip file found (typically there's only one)
                nested_zip_name = zip_files[0]
                print(f"Detected nested zip: {nested_zip_name}")

                # Extract ONLY the zip file content into memory
                try:
                    with zf.open(nested_zip_name) as nested_zip_file:
                        nested_zip_data = nested_zip_file.read()

                    # Process files within the nested zip in memory
                    with zipfile.ZipFile(io.BytesIO(nested_zip_data)) as inner_zf:
                        nested_files = [info.filename for info in inner_zf.infolist() if not info.is_dir()]

                except Exception as e:
                    error_msg = f"Failed to read nested zip '{nested_zip_name}': {e}"
                    print(f"Warning: {error_msg}")
                    errors.append((nested_zip_name, error_msg))
                    # Fall back to processing main zip files
                    nested_zip_data = None
                    nested_files = []

            # Use nested zip files if available, otherwise use main zip files
            if nested_files and nested_zip_data is not None:
                # Process files from nested zip in memory directly between the two zips
                # All nested files share the same zip data

                # Strip common top-level directory prefix to place files directly in zip root
                common_prefix = ""
                if nested_files:
                    # Find common directory prefix (e.g., 'Export-2023-11-17/')
                    common_prefix = os.path.commonprefix(nested_files)
                    # Ensure it's a complete directory path (ends with / and all files start with it)
                    if common_prefix and common_prefix.endswith('/') and all(f.startswith(common_prefix) for f in nested_files):
                        print(f"Stripping common directory prefix: {common_prefix.rstrip('/')}")
                    else:
                        common_prefix = ""

                # file_entries: (processed_path, zip_data, original_path)
                # processed_path is used for renaming logic, original_path for reading from zip
                file_entries = [(f[len(common_prefix):] if common_prefix else f, nested_zip_data, f) for f in nested_files]
            else:
                # Use main zip files: (processed_path, zip_data, original_path)
                file_entries = [(f, None, f) for f in all_files]

            # Initialize renamer
            renamer = NotionExportRenamer(filename_too_long_tracker=filename_too_long)

            # Build proposed renames
            proposed: Dict[str, str] = {}
            for rel_path, _, _ in file_entries:
                new_path = renamer.rename_path(rel_path)
                proposed[rel_path] = new_path

            # Detect duplicates
            proposed_to_originals = defaultdict(list)
            for orig, prop in proposed.items():
                proposed_to_originals[prop].append(orig)
            for prop, origs in proposed_to_originals.items():
                if len(origs) > 1:
                    duplicates[prop] = origs

            # Handle collisions
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

            # Final mapping with collision resolution
            final_map: Dict[str, str] = {}
            for rel_path, _, _ in file_entries:
                proposed_path = proposed[rel_path]
                parent, fname = os.path.split(proposed_path)
                final_fname = reserve(parent, fname)
                final_map[rel_path] = os.path.join(parent, final_fname)

            # Write output zip with renamed files and fixed links
            with zipfile.ZipFile(new_zip_path, "w", zipfile.ZIP_DEFLATED) as out_zf:
                for rel_path, zip_data, original_path in file_entries:
                    final_path = final_map[rel_path]

                    try:
                        # Read file content
                        if zip_data is not None:
                            # Read from nested zip in memory using original path
                            with zipfile.ZipFile(io.BytesIO(zip_data)) as inner_zf:
                                with inner_zf.open(original_path) as file_obj:
                                    file_content = file_obj.read()
                        else:
                            # Read from main zip
                            with zf.open(rel_path) as file_obj:
                                file_content = file_obj.read()

                        if rel_path.lower().endswith(".md"):
                            # Process markdown content
                            try:
                                md_content = file_content.decode("utf-8")
                                md_content = md_file_rewrite(renamer, rel_path, md_content, final_map)
                                file_content = md_content.encode("utf-8")
                            except UnicodeDecodeError as e:
                                error_msg = f"Failed to decode markdown '{rel_path}': {e}"
                                print(f"Warning: {error_msg}")
                                errors.append((rel_path, error_msg))
                            except Exception as e:
                                error_msg = f"Failed to process markdown '{rel_path}': {e}"
                                print(f"Warning: {error_msg}")
                                errors.append((rel_path, error_msg))

                        # Write to output zip
                        zi = zipfile.ZipInfo(_normalize_zip_path(final_path))
                        _ensure_zip_parent_dirs(out_zf, final_path, zi.date_time)
                        out_zf.writestr(zi, file_content)

                    except Exception as e:
                        error_msg = f"Failed to process file '{rel_path}': {e}"
                        print(f"Warning: {error_msg}")
                        errors.append((rel_path, error_msg))
                        continue

    # Write log file
    with open(log_file_path, 'w', encoding='utf-8') as log_f:
        if filename_too_long:
            log_f.write("FILENAME TOO LONG (truncated to 200 chars):\n")
            for path, original_name, length in filename_too_long:
                log_f.write(f"{path}: {original_name} ({length} chars)\n")
            log_f.write("\n")
        if errors:
            log_f.write("WITH ERROR:\n")
            for path, err in errors:
                log_f.write(f"{path}: {err}\n")
            log_f.write("\n")
        if duplicates:
            log_f.write("DUPLICATE files:\n")
            for prop_path, orig_paths in duplicates.items():
                log_f.write(f"Proposed name: {prop_path}\n")
                for orig in orig_paths:
                    log_f.write(f"  {orig}\n")
                log_f.write("\n")
        

    print(f"Output written to: {new_zip_path}")
    print(f"Log written to: {log_file_path}")
    return new_zip_path


def main(argv: Optional[Iterable[str]] = None) -> None:
    """
    CLI entrypoint.
    """
    parser = argparse.ArgumentParser(description="Fixes Notion export zip files by removing IDs and fixing links.")
    parser.add_argument("zip_path", type=str, help="Path to Notion exported .zip file")
    parser.add_argument(
        "--use-disk-extraction",
        action="store_true",
        help="Extract files to disk before processing (original method). "
             "By default, files are processed directly from zip to avoid path length issues."
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    import time
    start_time = time.time()

    try:
        out_file = process_notion_zip(args.zip_path, use_disk_extraction=args.use_disk_extraction)
        print(f"--- Finished in {time.time() - start_time:.2f} seconds ---")
        print(f"Output file: {out_file}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
