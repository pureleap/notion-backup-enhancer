"""
Tests for export_fix.py functionality.
"""

import io
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from export_fix import process_notion_zip


class TestExportFix:
    """Test cases for the export fix functionality."""

    def test_process_test_data_zip(self):
        """Test processing the provided test data ZIP file."""
        # Path to the test data
        test_zip_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "test-data",
            "auto-relation-demo-workspace.zip",
        )

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
            assert os.path.exists(output_zip_path), (
                f"Output zip not created at {output_zip_path}"
            )

            # Check contents of the output zip
            with zipfile.ZipFile(output_zip_path, "r") as zf:
                # Get list of all files and directories in the zip
                all_files = set(zf.namelist())

                # Check for required files in root
                assert "Home.md" in all_files, "Home.md not found in root of output zip"
                assert "Tasks.csv" in all_files, (
                    "Tasks.csv not found in root of output zip"
                )

                # Check for Tasks directory (directories end with /)
                tasks_dir_found = any(
                    name.startswith("Tasks/") and name.endswith("/")
                    for name in all_files
                )
                assert tasks_dir_found, (
                    "Tasks/ directory not found in root of output zip"
                )

                # Additional validation - ensure we have some content
                assert len(all_files) > 0, "Output zip appears to be empty"

                print(f"Output zip contains {len(all_files)} entries")
                print("Found expected files:")
                for expected in ["Home.md", "Tasks.csv"]:
                    if expected in all_files:
                        print(f"  ✓ {expected}")
                if tasks_dir_found:
                    print("  ✓ Tasks/ directory")

    def _make_embedded_zip(self, name: str = "attachment.zip") -> bytes:
        """Create a synthetic embedded zip attachment (like a user-uploaded file)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("program.exe", b"MZ binary data")
            z.writestr("readme.txt", b"embedded readme")
        return buf.getvalue()

    def _make_wrapper_zip(self, export_id: str) -> bytes:
        """Create a synthetic Notion export wrapper zip containing pages."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(
                f"Export-{export_id}/Export-{export_id}/Home.md",
                "# Home\n\n[Page](Other%2044b0c8d7.md)",
            )
            z.writestr(f"Export-{export_id}/Export-{export_id}/Tasks.csv", "a,b\n1,2\n")
        return buf.getvalue()

    def _make_outer_zip(self, wrapper_data: bytes, export_id: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"Export-{export_id}/Export-{export_id}.zip", wrapper_data)
        return buf.getvalue()

    @staticmethod
    def _write_and_process(outer: bytes) -> zipfile.ZipFile:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = os.path.join(temp_dir, "input.zip")
            with open(input_path, "wb") as f:
                f.write(outer)
            output = process_notion_zip(input_path)
            with open(output, "rb") as f:
                data = f.read()
        return zipfile.ZipFile(io.BytesIO(data))

    def test_embedded_zip_kept_as_zip_and_pages_preserved(self):
        """Regression: embedded page-attached zips must not replace the whole export."""
        export_id = "3d6a72ce-d218-4396-b162-536d999086a6"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"Export-{export_id}/Home.md", "# Home\n")
            z.writestr(f"Export-{export_id}/Pages/Note c31b37a8.md", "# Note\n")
            z.writestr(
                f"Export-{export_id}/Notebook/Pages/Incipitor Add Shortcuts/"
                "Incipitor.zip",
                self._make_embedded_zip(),
            )
        zf = self._write_and_process(buf.getvalue())
        names = zf.namelist()
        assert len(names) >= 3, f"Expected pages + embedded zip, got {names}"
        assert any(n.endswith(".md") and n.endswith("Home.md") for n in names), (
            f"Home.md page missing from output: {names}"
        )
        assert any(
            "Notebook/Pages/" in n and n.endswith("Incipitor.zip") for n in names
        ), f"Embedded zip not preserved as zip: {names}"
        # Embedded zip content must not be extracted to the zip root
        assert "program.exe" not in names and "readme.txt" not in names

    def test_identical_name_duplicates_get_distinct_paths(self):
        """Identical repeated entries must not overwrite each other in output."""
        file_name = "Quotes c09743d178b04a50b7123c1304394ef2.md"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i in range(1, 6):
                z.writestr(file_name, f"# Quotes copy {i}\n".encode("utf-8"))
        zf = self._write_and_process(buf.getvalue())
        names = zf.namelist()
        file_names = [n for n in names if not n.endswith("/")]
        assert len(file_names) == 5, f"Expected 5 output files, got {file_names}"
        assert len(set(file_names)) == 5, f"Duplicate output names: {file_names}"
        # All copies must retain their distinct content
        contents = {zf.read(n).decode("utf-8") for n in file_names}
        assert contents == {f"# Quotes copy {i}\n" for i in range(1, 6)}

    def test_top_level_dir_stripped_without_wrapper(self):
        """A flat export (no wrapper) must strip its top-level folder."""
        export_id = "3d6a72ce-d218-4396-b162-536d999086a6"
        prefix = f"Export-{export_id}"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"{prefix}/Home.md", "# Home\n")
            z.writestr(f"{prefix}/Tasks.csv", "a,b\n1,2\n")
        zf = self._write_and_process(buf.getvalue())
        names = zf.namelist()
        assert "Home.md" in names, f"Home.md should be at zip root, got {names}"
        assert "Tasks.csv" in names, f"Tasks.csv should be at zip root, got {names}"
        assert not any(n.startswith(prefix + "/") for n in names), names

    def test_export_wrapper_zip_still_unwrapped(self):
        """A genuine Notion export wrapper zip should still be unwrapped."""
        export_id = "b8f52cdf-c19e-4827-8fd2-5385108bebd6"
        outer = self._make_outer_zip(self._make_wrapper_zip(export_id), export_id)
        zf = self._write_and_process(outer)
        names = zf.namelist()
        assert any(n.endswith("Home.md") for n in names), names
        assert any(n.endswith("Tasks.csv") for n in names), names
        assert not any(n.endswith(".zip") for n in names), names

    def test_embedded_zip_in_disk_mode_kept(self):
        """Disk extraction mode should also keep embedded zips as files."""
        export_id = "b8f52cdf-c19e-4827-8fd2-5385108bebd6"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            wrapper_buf = io.BytesIO()
            with zipfile.ZipFile(wrapper_buf, "w") as wz:
                wz.writestr("Export-" + export_id + "/Home.md", "# Home\n")
                wz.writestr(
                    "Export-" + export_id + "/Pages/My Page/render.zip",
                    self._make_embedded_zip(),
                )
            z.writestr(
                f"Export-{export_id}/Export-{export_id}.zip",
                wrapper_buf.getvalue(),
            )
        zf = self._write_and_process(buf.getvalue())
        names = zf.namelist()
        assert any(n.endswith("Home.md") for n in names), names
        assert any(
            n.startswith("Pages/") and n.endswith("render.zip") for n in names
        ), names
