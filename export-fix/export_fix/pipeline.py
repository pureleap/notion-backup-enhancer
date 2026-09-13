"""Core pipeline: enumerates, renames, collision-resolves and writes the fixed export."""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .links import md_file_rewrite
from .naming import NotionExportRenamer, _extract_short_id_from_path
from .ziputils import (
    _ensure_zip_parent_dirs,
    _is_export_wrapper,
    _is_zip,
    _normalize_zip_path,
    _read_nested_zip,
    _set_zip_permissions,
    _strip_common_dir_prefix,
)


@dataclass
class FileEntry:
    """One file to be written to the output zip.

    processed_path: path used for renaming logic (relative paths, backslashes ok).
    reader: callable returning the file's bytes.
    """

    processed_path: str
    reader: Callable[[], bytes]


def _output_paths(zip_path: str) -> Tuple[str, str, str]:
    """Compute (input_dir, new_zip_path, log_file_path) for an input zip."""
    input_dir = os.path.dirname(os.path.abspath(zip_path))
    if not input_dir:
        input_dir = "."  # If no directory, use current directory

    zip_name = os.path.basename(zip_path)
    if zip_name.lower().endswith(".zip"):
        base_name = zip_name[:-4]  # Remove .zip extension
    else:
        base_name = zip_name
    new_zip_path = os.path.join(input_dir, f"{base_name}.fixed.zip")
    log_file_path = os.path.join(input_dir, f"{base_name}.log.txt")
    return input_dir, new_zip_path, log_file_path


def _warn(errors: List[Tuple[str, str]], path: str, message: str) -> None:
    print(f"Warning: {message}")
    errors.append((path, message))


# ---------------------------------------------------------------------------
# Collection: build FileEntry lists for the two processing modes
# ---------------------------------------------------------------------------


def _collect_from_disk(
    zip_path: str,
) -> Tuple[List[FileEntry], List[Tuple[str, str]]]:
    """Disk-extraction mode (original method)."""
    errors: List[Tuple[str, str]] = []
    entries: List[FileEntry] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                try:
                    zf.extract(info, tmp_dir)
                except Exception as e:
                    _warn(
                        errors,
                        info.filename,
                        f"Failed to extract '{info.filename}': {e}",
                    )

        # Handle nested zip (Notion sometimes wraps the export in another zip)
        top_entries = list(os.listdir(tmp_dir))
        unwrapped_wrapper_path: str = ""
        if len(top_entries) == 1 and top_entries[0].lower().endswith(".zip"):
            unwrapped_wrapper_path = os.path.join(tmp_dir, top_entries[0])
            print(f"Detected nested export wrapper zip: {unwrapped_wrapper_path}")
            with zipfile.ZipFile(unwrapped_wrapper_path) as inner_zf:
                for info in inner_zf.infolist():
                    try:
                        inner_zf.extract(info, tmp_dir)
                    except Exception as e:
                        _warn(
                            errors,
                            info.filename,
                            f"Failed to extract from nested zip '{info.filename}': {e}",
                        )

        # First pass: enumerate files
        # Skip the export wrapper zip that was already unwrapped,
        # but keep embedded zip attachments as zip files
        file_paths: List[Tuple[str, str]] = []
        for root, _dirs, files in os.walk(tmp_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                if unwrapped_wrapper_path and abs_path == unwrapped_wrapper_path:
                    continue
                try:
                    rel_path = os.path.relpath(abs_path, tmp_dir)
                    file_paths.append((rel_path, abs_path))
                except (OSError, ValueError) as e:
                    _warn(
                        errors,
                        abs_path,
                        f"Skipping file due to path error: {abs_path} - {e}",
                    )

        for rel_path, abs_path in file_paths:
            entries.append(
                FileEntry(
                    processed_path=rel_path,
                    reader=(lambda p=abs_path: open(p, "rb").read()),
                )
            )

    return entries, errors


def _collect_from_zip(
    zf: zipfile.ZipFile,
) -> Tuple[List[FileEntry], List[Tuple[str, str]]]:
    """Zip-to-zip mode (resilient to path length issues).

    The passed ZipFile must remain open until the returned readers are used.
    """
    errors: List[Tuple[str, str]] = []
    entries: List[FileEntry] = []

    # Get all file paths from the main zip
    all_infos = [info for info in zf.infolist() if not info.is_dir()]
    all_files = [info.filename for info in all_infos]

    # Check for nested export wrapper zip (Notion sometimes wraps the
    # export in another zip). Embedded zip attachments inside pages are
    # NOT wrappers and are kept as zip files in the output.
    nested_zip_data: Optional[bytes] = None
    nested_zip_name: Optional[str] = None
    nested_files: List[str] = []

    # Look for a zip file that qualifies as an export wrapper
    zip_candidates = [f for f in all_files if f.lower().endswith(".zip")]
    for candidate in zip_candidates:
        try:
            with zf.open(candidate) as nested_zip_file:
                candidate_data = nested_zip_file.read()
        except Exception as e:
            _warn(errors, candidate, f"Failed to read nested zip '{candidate}': {e}")
            continue

        if _is_export_wrapper(candidate, candidate_data):
            nested_zip_name = candidate
            nested_zip_data = candidate_data
            break

    if nested_zip_name is not None:
        print(f"Detected nested export wrapper zip: {nested_zip_name}")

        # Process files within the nested zip in memory
        try:
            nested_files = _read_nested_zip(nested_zip_name, nested_zip_data or b"")
        except Exception as e:
            _warn(
                errors,
                nested_zip_name,
                f"Failed to read nested zip '{nested_zip_name}': {e}",
            )
            # Fall back to processing main zip files
            nested_zip_data = None
            nested_files = []
    elif zip_candidates:
        print(
            f"Found {len(zip_candidates)} embedded zip file(s); "
            "keeping them as zip files in the output"
        )

    if nested_zip_data is not None and nested_files:
        # Process files from nested zip in memory directly between the two zips.
        # Strip common top-level directory prefix to place files directly in zip root
        common_prefix = _strip_common_dir_prefix(nested_files)
        if common_prefix:
            print(f"Stripping common directory prefix: {common_prefix.rstrip('/')}")

        for f in nested_files:
            processed = f[len(common_prefix) :] if common_prefix else f
            entries.append(
                FileEntry(
                    processed_path=processed,
                    # Read from nested zip in memory using original path
                    reader=(
                        lambda d=nested_zip_data, p=f: (
                            zipfile.ZipFile(io.BytesIO(d)).open(p).read()
                        )
                    ),
                )
            )
    else:
        # Use main zip files.
        # Strip a common top-level directory (e.g. 'Export-<uuid>/') so
        # pages sit at the zip root even without a nested wrapper zip
        common_prefix = _strip_common_dir_prefix(all_files)
        if common_prefix:
            print(f"Stripping common directory prefix: {common_prefix.rstrip('/')}")

        for info in all_infos:
            processed = (
                info.filename[len(common_prefix) :] if common_prefix else info.filename
            )
            entries.append(
                FileEntry(
                    processed_path=processed,
                    # Read from main zip via the entry's ZipInfo so duplicate
                    # entries are not collapsed to their first occurrence
                    reader=(lambda z=zf, i=info: z.open(i).read()),
                )
            )

    return entries, errors


# ---------------------------------------------------------------------------
# Rename proposal, duplicate detection and collision resolution
# ---------------------------------------------------------------------------


def _build_proposed(
    file_entries: List[FileEntry], renamer: NotionExportRenamer
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """Build the rename mapping and detect duplicate proposed paths.

    Returns (proposed, duplicates):
        proposed: original path -> proposed path
        duplicates: proposed path -> list of original paths colliding on it
    """
    proposed: Dict[str, str] = {}
    proposed_to_originals: Dict[str, List[str]] = defaultdict(list)
    for entry in file_entries:
        rel_path = entry.processed_path
        prop = renamer.rename_path(rel_path)
        proposed.setdefault(rel_path, prop)
        proposed_to_originals[prop].append(rel_path)

    duplicates = {
        prop: origs for prop, origs in proposed_to_originals.items() if len(origs) > 1
    }
    return proposed, duplicates


def _resolve_collisions(
    file_entries: List[FileEntry], proposed: Dict[str, str]
) -> Tuple[List[str], Dict[str, str], Dict[str, List[str]]]:
    """Resolve output-path collisions deterministically.

    reserve() is called per file entry so that entries with identical
    original paths (Notion sometimes duplicates a page entry) still
    receive distinct output paths instead of overwriting each other.

    Returns (final_paths, final_map, duplicate_resolved).
    """
    registry = set()

    def reserve(filename: str, original_path: str = "") -> str:
        if filename not in registry:
            registry.add(filename)
            return filename
        name_no_ext, ext = os.path.splitext(filename)
        short_id = _extract_short_id_from_path(original_path)
        if short_id:
            cand = f"{name_no_ext} {short_id}{ext}"
            if cand not in registry:
                registry.add(cand)
                return cand
        i = 1
        while True:
            cand = f"{name_no_ext} ({i}){ext}"
            if cand not in registry:
                registry.add(cand)
                return cand
            i += 1

    final_paths: List[str] = []
    final_map: Dict[str, str] = {}  # original path -> final (for link rewriting)
    for entry in file_entries:
        rel_path = entry.processed_path
        proposed_path = proposed[rel_path]
        parent, fname = os.path.split(proposed_path)
        final_fname = reserve(fname, rel_path)
        final = os.path.join(parent, final_fname)
        final_paths.append(final)
        final_map[rel_path] = final

    # Track resolved (final) name per proposed name so the duplicate
    # log can show how name collisions were resolved
    duplicate_resolved: Dict[str, List[str]] = defaultdict(list)
    for entry, final in zip(file_entries, final_paths):
        duplicate_resolved[proposed[entry.processed_path]].append(final)

    return final_paths, final_map, duplicate_resolved


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------


def _write_output_zip(
    new_zip_path: str,
    file_entries: List[FileEntry],
    final_paths: List[str],
    final_map: Dict[str, str],
    renamer: NotionExportRenamer,
    errors: List[Tuple[str, str]],
) -> None:
    """Write output zip with renamed files and fixed links."""
    emitted_dirs: set = set()
    with zipfile.ZipFile(new_zip_path, "w", zipfile.ZIP_DEFLATED) as out_zf:
        for final_path, entry in zip(final_paths, file_entries):
            rel_path = entry.processed_path
            try:
                file_content = entry.reader()

                if rel_path.lower().endswith(".md"):
                    # Process markdown content
                    try:
                        md_content = file_content.decode("utf-8")
                        md_content = md_file_rewrite(
                            renamer, rel_path, md_content, final_map
                        )
                        file_content = md_content.encode("utf-8")
                    except UnicodeDecodeError as e:
                        _warn(
                            errors,
                            rel_path,
                            f"Failed to decode markdown '{rel_path}': {e}",
                        )
                    except Exception as e:
                        _warn(
                            errors,
                            rel_path,
                            f"Failed to process markdown '{rel_path}': {e}",
                        )

                # Write to output zip
                zi = zipfile.ZipInfo(_normalize_zip_path(final_path))
                _set_zip_permissions(zi)
                _ensure_zip_parent_dirs(out_zf, final_path, zi.date_time, emitted_dirs)
                out_zf.writestr(zi, file_content)
            except Exception as e:
                _warn(
                    errors,
                    rel_path,
                    f"Failed to process file '{rel_path}' to output zip: {e}",
                )
                continue


def _write_log(
    log_file_path: str,
    errors: List[Tuple[str, str]],
    duplicates: Dict[str, List[str]],
    filename_too_long: List[Tuple[str, str, int]],
    duplicate_resolved: Dict[str, List[str]],
) -> None:
    with open(log_file_path, "w", encoding="utf-8") as log_f:
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
                resolved = duplicate_resolved.get(prop_path, [])
                if len(resolved) > 1:
                    log_f.write("  Resolved to:\n")
                    for final in resolved:
                        log_f.write(f"    {final}\n")
                log_f.write("\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run_pipeline(
    new_zip_path: str,
    log_file_path: str,
    file_entries: List[FileEntry],
    filename_too_long: List,
    errors: List[Tuple[str, str]],
) -> str:
    renamer = NotionExportRenamer(filename_too_long_tracker=filename_too_long)
    proposed, duplicates = _build_proposed(file_entries, renamer)
    final_paths, final_map, duplicate_resolved = _resolve_collisions(
        file_entries, proposed
    )
    _write_output_zip(
        new_zip_path, file_entries, final_paths, final_map, renamer, errors
    )
    _write_log(log_file_path, errors, duplicates, filename_too_long, duplicate_resolved)
    print(f"Output written to: {new_zip_path}")
    print(f"Log written to: {log_file_path}")
    return new_zip_path


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

    _, new_zip_path, log_file_path = _output_paths(zip_path)

    print(f"Processing '{zip_path}'...")
    if use_disk_extraction:
        print("Using disk extraction mode (original method)")
        file_entries, errors = _collect_from_disk(zip_path)
        return _run_pipeline(new_zip_path, log_file_path, file_entries, [], errors)

    print("Using zip-to-zip processing mode (resilient to path length issues)")
    with zipfile.ZipFile(zip_path) as zf:
        file_entries, errors = _collect_from_zip(zf)
        return _run_pipeline(new_zip_path, log_file_path, file_entries, [], errors)
