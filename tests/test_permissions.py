"""Test cases for the permissions column functionality (Part A of plan-pane-tabs-and-permissions.md)."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from surftp.fs import LocalFileSystem, format_permissions
from surftp.fs.types import FileEntry


class TestFormatPermissions:
    """Test the format_permissions function."""

    def test_format_permissions_directory(self):
        """Test formatting directory permissions."""
        # drwxr-xr-x (0o40755)
        assert format_permissions(0o40755, True) == "drwxr-xr-x"
        # drwx------ (0o40700)
        assert format_permissions(0o40700, True) == "drwx------"
        # drwxrwxrwx (0o40777)
        assert format_permissions(0o40777, True) == "drwxrwxrwx"

    def test_format_permissions_regular_file(self):
        """Test formatting regular file permissions."""
        # -rw-r--r-- (0o100644)
        assert format_permissions(0o100644, False) == "-rw-r--r--"
        # -rwxr-xr-x (0o100755)
        assert format_permissions(0o100755, False) == "-rwxr-xr-x"
        # -rw------- (0o100600)
        assert format_permissions(0o100600, False) == "-rw-------"

    def test_format_permissions_setuid(self):
        """Test formatting setuid bit."""
        # -rwsr-xr-x (0o104755)
        assert format_permissions(0o104755, False) == "-rwsr-xr-x"
        # -rwSr-xr-x (0o104655) - setuid but no execute
        assert format_permissions(0o104655, False) == "-rwSr-xr-x"

    def test_format_permissions_setgid(self):
        """Test formatting setgid bit."""
        # -rwxr-sr-x (0o102755)
        assert format_permissions(0o102755, False) == "-rwxr-sr-x"
        # -rwxr-Sr-x (0o102745) - setgid but no group execute
        assert format_permissions(0o102745, False) == "-rwxr-Sr-x"

    def test_format_permissions_sticky(self):
        """Test formatting sticky bit."""
        # drwxrwxrwt (0o41777)
        assert format_permissions(0o41777, True) == "drwxrwxrwt"
        # drwxrwxrwT (0o41776) - sticky but no other execute
        assert format_permissions(0o41776, True) == "drwxrwxrwT"

    def test_format_permissions_symlink(self):
        """Test formatting symlink permissions."""
        # lrwxrwxrwx (0o120777)
        assert format_permissions(0o120777, False) == "lrwxrwxrwx"

    def test_format_permissions_zero(self):
        """Test formatting zero permissions (unknown)."""
        # Should return empty string for 0
        assert format_permissions(0, False) == ""
        assert format_permissions(0, True) == ""

    def test_format_permissions_no_permission_bits(self):
        """Test formatting with no permission bits."""
        # ---------- (0o100000)
        assert format_permissions(0o100000, False) == "----------"
        # d--------- (0o40000)
        assert format_permissions(0o40000, True) == "d---------"


class TestLocalFileSystemPermissions:
    """Test that LocalFileSystem correctly reports permissions."""

    @pytest.mark.asyncio
    async def test_list_directory_includes_permissions(self):
        """Test that list_directory includes permissions in FileEntry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file with specific permissions
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("test")
            os.chmod(test_file, 0o644)

            # Create a directory with specific permissions
            test_dir = Path(tmpdir) / "testdir"
            test_dir.mkdir()
            os.chmod(test_dir, 0o755)

            fs = LocalFileSystem()
            entries = await fs.list_directory(tmpdir)

            # Find our test entries
            file_entry = next(e for e in entries if e.name == "test.txt")
            dir_entry = next(e for e in entries if e.name == "testdir")

            # Check that permissions are set (not 0)
            assert file_entry.permissions != 0
            assert dir_entry.permissions != 0

            # Check that permissions match what we set
            # Note: The full mode includes file type bits
            assert file_entry.permissions & 0o777 == 0o644
            assert dir_entry.permissions & 0o777 == 0o755

    @pytest.mark.asyncio
    async def test_stat_includes_permissions(self):
        """Test that stat includes permissions in FileEntry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("test")
            os.chmod(test_file, 0o755)

            fs = LocalFileSystem()
            entry = await fs.stat(str(test_file))

            assert entry.permissions != 0
            assert entry.permissions & 0o777 == 0o755

    @pytest.mark.asyncio
    async def test_parent_entry_has_zero_permissions(self):
        """Test that the .. entry has permissions=0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFileSystem()
            entries = await fs.list_directory(tmpdir)

            # Find the .. entry
            parent_entry = next(e for e in entries if e.name == "..")

            # Parent entry should have permissions=0
            assert parent_entry.permissions == 0


class TestIntegration:
    """Integration tests for permissions in the UI."""

    @pytest.mark.asyncio
    async def test_pilot_permissions_display(self):
        """Test that permissions are displayed correctly in the UI."""
        from surftp.app import SurfFTPApp

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files with different permissions
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("test")
            os.chmod(test_file, 0o644)

            test_dir = Path(tmpdir) / "testdir"
            test_dir.mkdir()
            os.chmod(test_dir, 0o755)

            app = SurfFTPApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                left = app.left_pane
                left.path = tmpdir
                await pilot.pause()

                # Check that the Mode column shows 10-character strings
                for i in range(left.table.row_count):
                    mode_cell = left.table.get_cell_at((i, 3))
                    name_cell = left.table.get_cell_at((i, 0))
                    if name_cell == "..":
                        # Parent entry should have empty mode
                        assert mode_cell == "", f"Expected empty for .., got {mode_cell!r}"
                    else:
                        # Other entries should have 10-character mode strings
                        assert len(mode_cell) == 10, f"Expected 10 chars for {name_cell}, got {len(mode_cell)}: {mode_cell!r}"
                        # First character should be a type indicator
                        assert mode_cell[0] in "dlbcsp-", f"Expected type char for {name_cell}, got {mode_cell[0]!r}"


if __name__ == "__main__":
    # Run synchronous tests
    test_format = TestFormatPermissions()
    test_format.test_format_permissions_directory()
    test_format.test_format_permissions_regular_file()
    test_format.test_format_permissions_setuid()
    test_format.test_format_permissions_setgid()
    test_format.test_format_permissions_sticky()
    test_format.test_format_permissions_symlink()
    test_format.test_format_permissions_zero()
    test_format.test_format_permissions_no_permission_bits()
    print("All format_permissions tests passed!")

    async def run_async_tests():
        test_fs = TestLocalFileSystemPermissions()
        await test_fs.test_list_directory_includes_permissions()
        await test_fs.test_stat_includes_permissions()
        await test_fs.test_parent_entry_has_zero_permissions()
        print("All LocalFileSystem permissions tests passed!")

        test_int = TestIntegration()
        await test_int.test_pilot_permissions_display()
        print("All integration tests passed!")

    asyncio.run(run_async_tests())
    print("\nALL PERMISSIONS TESTS PASSED")
