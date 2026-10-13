#!/usr/bin/env python3
"""Command line interface for the Notion export fixer.

CLI:
  notion-export-fix <zip_path> [--use-disk-extraction]

Or, without installing the package:
  python -m export_fix.cli <zip_path> [--use-disk-extraction]
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Iterable, Optional

from .pipeline import process_notion_zip


def main(argv: Optional[Iterable[str]] = None) -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Fixes Notion export zip files by removing IDs and fixing links."
    )
    parser.add_argument("zip_path", type=str, help="Path to Notion exported .zip file")
    parser.add_argument(
        "--use-disk-extraction",
        action="store_true",
        help="Extract files to disk before processing (original method). "
        "By default, files are processed directly from the zip to avoid "
        "path-length issues.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    start_time = time.time()

    try:
        out_file = process_notion_zip(
            args.zip_path, use_disk_extraction=args.use_disk_extraction
        )
        print(f"--- Finished in {time.time() - start_time:.2f} seconds ---")
        print(f"Output file: {out_file}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
