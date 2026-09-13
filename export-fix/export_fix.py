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
import sys
from typing import Iterable, Optional

# Resolve to the export_fix package (not this same-named script)
from importlib import import_module

_process = import_module("export_fix.pipeline")


def main(argv: Optional[Iterable[str]] = None) -> None:
    """
    CLI entrypoint.
    """
    parser = argparse.ArgumentParser(
        description="Fixes Notion export zip files by removing IDs and fixing links."
    )
    parser.add_argument("zip_path", type=str, help="Path to Notion exported .zip file")
    parser.add_argument(
        "--use-disk-extraction",
        action="store_true",
        help="Extract files to disk before processing (original method). "
        "By default, files are processed directly from zip to avoid path length issues.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    import time

    start_time = time.time()

    try:
        out_file = _process.process_notion_zip(
            args.zip_path, use_disk_extraction=args.use_disk_extraction
        )
        print(f"--- Finished in {time.time() - start_time:.2f} seconds ---")
        print(f"Output file: {out_file}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
