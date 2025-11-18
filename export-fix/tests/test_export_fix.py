"""
Tests for export_fix.py functionality.
"""
import os
import tempfile
import zipfile
from pathlib import Path

import pytest

# Import the module under test
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from export_fix import process_notion_zip


class TestExportFix:
    """Test cases for the export fix functionality."""

    def test_process_test_data_zip(self):
        """Test processing the provided test data ZIP file."""
        # Path to the test data
        test_zip_path = os.path.join(os.path.dirname(__file__), "..", "test-data", "auto-relation-demo-workspace.zip")

        # Ensure test data exists
        assert os.path.exists(test_zip_path), f"Test data not found at {test_zip_path}"

        # Create a temporary directory for output
        with tempfile.TemporaryDirectory() as temp_dir:
            # Copy test zip to temp directory to avoid modifying original
            temp_zip_path = os.path.join(temp_dir, "test_input.zip")
            import shutil
            shutil.copy2(test_zip_path, temp_zip_path)

            # Process the zip file
            output_zip_path = process_notion_zip(temp_zip_path)

            # Verify output zip was created
            assert os.path.exists(output_zip_path), f"Output zip not created at {output_zip_path}"

            # Check contents of the output zip
            with zipfile.ZipFile(output_zip_path, 'r') as zf:
                # Get list of all files and directories in the zip
                all_files = set(zf.namelist())

                # Check for required files in root
                assert "Home.md" in all_files, "Home.md not found in root of output zip"
                assert "Tasks.csv" in all_files, "Tasks.csv not found in root of output zip"

                # Check for Tasks directory (directories end with /)
                tasks_dir_found = any(name.startswith("Tasks/") and name.endswith("/") for name in all_files)
                assert tasks_dir_found, "Tasks/ directory not found in root of output zip"

                # Additional validation - ensure we have some content
                assert len(all_files) > 0, "Output zip appears to be empty"

                print(f"Output zip contains {len(all_files)} entries")
                print("Found expected files:")
                for expected in ["Home.md", "Tasks.csv"]:
                    if expected in all_files:
                        print(f"  ✓ {expected}")
                if tasks_dir_found:
                    print("  ✓ Tasks/ directory")
