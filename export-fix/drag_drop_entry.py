#!/usr/bin/env python3
"""
Drag-and-drop entry point for Notion Backup Enhancer.

This script serves as the entry point for the Windows executable created by PyInstaller.
When a user drops a zip file on the executable, Windows passes the file path as a command line argument.
"""

import sys
import os
from pathlib import Path

# Import the main processing function
from export_fix import process_notion_zip


def _pause() -> None:
    try:
        input("Press Enter to exit...")
    except EOFError:
        pass


def main():
    if len(sys.argv) != 2:
        print("Usage: Drag and drop a Notion export zip file onto this executable.")
        print("Or run: NotionBackupEnhancer.exe <path_to_zip_file>")
        _pause()
        sys.exit(1)

    zip_path = sys.argv[1]

    # Check if the file exists
    if not os.path.exists(zip_path):
        print(f"Error: File not found: {zip_path}")
        _pause()
        sys.exit(1)

    # Check if it's a zip file
    if not zip_path.lower().endswith(".zip"):
        print(f"Error: File must be a .zip file: {zip_path}")
        _pause()
        sys.exit(1)

    try:
        print(f"Processing: {zip_path}")
        output_path = process_notion_zip(zip_path)
        print(f"Success! Output created: {output_path}")

        # Get the directory of the output file for convenience
        output_dir = os.path.dirname(output_path)
        print(f"Files created in: {output_dir}")

    except Exception as e:
        print(f"Error processing file: {e}")
        _pause()
        sys.exit(1)

    # Keep console open so user can see the results
    _pause()


if __name__ == "__main__":
    main()
