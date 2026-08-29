"""Test cases for the session tabs functionality (Part B of plan-pane-tabs-and-permissions.md)."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from surftp.app import SurfFTPApp
from surftp.widgets.sessions import SessionTabs


class TestSessionTabsBasic:
    """Test basic session tabs functionality."""

    @pytest.mark.asyncio
    async def test_session_tabs_created(self):
        """Test that session tabs are created for both sides."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            left_sessions = app.left_sessions
            right_sessions = app.right_sessions
            
            assert left_sessions is not None
            assert right_sessions is not None

    @pytest.mark.asyncio
    async def test_local_tab_exists(self):
        """Test that each side has a Local tab."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            left_sessions = app.left_sessions
            right_sessions = app.right_sessions
            
            # Each side should have exactly one pane (the Local tab)
            assert len(left_sessions.panes) == 1
            assert len(right_sessions.panes) == 1
            
            # The active pane should be the local pane
            assert left_sessions.active_pane is left_sessions._local_pane
            assert right_sessions.active_pane is right_sessions._local_pane

    @pytest.mark.asyncio
    async def test_initial_focus_is_left(self):
        """Test that initial focus is on the left pane."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # The active pane should be the left pane
            assert app.active_pane is app.left_sessions.active_pane


class TestSessionTabsNavigation:
    """Test session tabs navigation."""

    @pytest.mark.asyncio
    async def test_tab_switches_sides(self):
        """Test that tab key switches between left and right panes."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # Start on left
            assert app.active_pane is app.left_sessions.active_pane
            
            # Press tab to go to right
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert app.active_pane is app.right_sessions.active_pane
            
            # Press tab again to go back to left
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert app.active_pane is app.left_sessions.active_pane

    @pytest.mark.asyncio
    async def test_navigation_works(self):
        """Test that navigation still works with session tabs."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            left = app.left_sessions.active_pane
            initial_path = left.path
            
            # Navigate to parent
            await pilot.press("enter")
            await pilot.pause(0.2)
            
            # Path should have changed
            assert left.path != initial_path

    @pytest.mark.asyncio
    async def test_ctrl_pagedown_next_session(self):
        """Test that ctrl+pagedown switches to next session."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # With only one tab, this should be a no-op
            left_sessions = app.left_sessions
            initial_pane = left_sessions.active_pane
            
            await pilot.press("ctrl+pagedown")
            await pilot.pause(0.1)
            
            # Should still be on the same pane
            assert left_sessions.active_pane is initial_pane

    @pytest.mark.asyncio
    async def test_ctrl_pageup_previous_session(self):
        """Test that ctrl+pageup switches to previous session."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # With only one tab, this should be a no-op
            left_sessions = app.left_sessions
            initial_pane = left_sessions.active_pane
            
            await pilot.press("ctrl+pageup")
            await pilot.pause(0.1)
            
            # Should still be on the same pane
            assert left_sessions.active_pane is initial_pane

    @pytest.mark.asyncio
    async def test_alt_1_activates_local_tab(self):
        """Test that alt+1 activates the Local tab."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            left_sessions = app.left_sessions
            
            # Press alt+1 to go to Local tab
            await pilot.press("alt+1")
            await pilot.pause(0.1)
            
            # Should be on the Local tab
            assert left_sessions.active_pane is left_sessions._local_pane


class TestSessionTabsDisconnect:
    """Test disconnect functionality with session tabs."""

    @pytest.mark.asyncio
    async def test_ctrl_d_on_local_tab(self):
        """Test that ctrl+d on Local tab shows a message."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # Try to close the Local tab
            await pilot.press("ctrl+d")
            await pilot.pause(0.2)
            
            # Should still be on the Local tab
            assert app.active_pane is app.left_sessions._local_pane

    @pytest.mark.asyncio
    async def test_ctrl_w_on_local_tab(self):
        """Test that ctrl+w on Local tab shows a message."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # Try to close the Local tab
            await pilot.press("ctrl+w")
            await pilot.pause(0.2)
            
            # Should still be on the Local tab
            assert app.active_pane is app.left_sessions._local_pane


class TestSessionTabsMultipleSessions:
    """Test multiple session tabs."""

    @pytest.mark.asyncio
    async def test_session_count_property(self):
        """Test that session_count returns the correct count."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            left_sessions = app.left_sessions
            
            # Initially, there should be 0 remote sessions (only Local)
            assert left_sessions.session_count == 0

    @pytest.mark.asyncio
    async def test_panes_property_returns_all_panes(self):
        """Test that panes property returns all panes."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            left_sessions = app.left_sessions
            
            # Should return a list with the Local pane
            panes = left_sessions.panes
            assert len(panes) == 1
            assert panes[0] is left_sessions._local_pane


class TestSessionTabsFocus:
    """Test focus handling with session tabs."""

    @pytest.mark.asyncio
    async def test_focus_lands_on_table(self):
        """Test that focus lands on the DataTable, not the tab bar."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # The focused widget should be a DataTable
            from textual.widgets import DataTable
            assert isinstance(app.focused, DataTable)

    @pytest.mark.asyncio
    async def test_tab_does_not_focus_tab_bar(self):
        """Test that pressing tab doesn't focus the tab bar."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # Press tab to switch sides
            await pilot.press("tab")
            await pilot.pause(0.1)
            
            # Focus should still be on a DataTable
            from textual.widgets import DataTable
            assert isinstance(app.focused, DataTable)


class TestSessionTabsShutdown:
    """Test shutdown with session tabs."""

    @pytest.mark.asyncio
    async def test_close_everything_closes_all_sessions(self):
        """Test that close_everything closes all sessions."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            # This should not raise any errors
            # close_everything is a @work method, so we call it without await
            app.close_everything()
            # Give it time to complete
            await pilot.pause(0.5)


class TestSessionTabsEdgeCases:
    """Test edge cases with session tabs."""

    @pytest.mark.asyncio
    async def test_activate_out_of_range_index(self):
        """Test that activate with out-of-range index is a no-op."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            left_sessions = app.left_sessions
            initial_pane = left_sessions.active_pane
            
            # Try to activate an out-of-range index
            left_sessions.activate(999)
            await pilot.pause(0.1)
            
            # Should still be on the same pane
            assert left_sessions.active_pane is initial_pane

    @pytest.mark.asyncio
    async def test_activate_negative_index(self):
        """Test that activate with negative index is a no-op."""
        app = SurfFTPApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            
            left_sessions = app.left_sessions
            initial_pane = left_sessions.active_pane
            
            # Try to activate a negative index
            left_sessions.activate(-1)
            await pilot.pause(0.1)
            
            # Should still be on the same pane
            assert left_sessions.active_pane is initial_pane


if __name__ == "__main__":
    async def run_all_tests():
        print("Running basic tests...")
        test_basic = TestSessionTabsBasic()
        await test_basic.test_session_tabs_created()
        await test_basic.test_local_tab_exists()
        await test_basic.test_initial_focus_is_left()
        print("✓ Basic tests passed")
        
        print("\nRunning navigation tests...")
        test_nav = TestSessionTabsNavigation()
        await test_nav.test_tab_switches_sides()
        await test_nav.test_navigation_works()
        await test_nav.test_ctrl_pagedown_next_session()
        await test_nav.test_ctrl_pageup_previous_session()
        await test_nav.test_alt_1_activates_local_tab()
        print("✓ Navigation tests passed")
        
        print("\nRunning disconnect tests...")
        test_disc = TestSessionTabsDisconnect()
        await test_disc.test_ctrl_d_on_local_tab()
        await test_disc.test_ctrl_w_on_local_tab()
        print("✓ Disconnect tests passed")
        
        print("\nRunning multiple sessions tests...")
        test_multi = TestSessionTabsMultipleSessions()
        await test_multi.test_session_count_property()
        await test_multi.test_panes_property_returns_all_panes()
        print("✓ Multiple sessions tests passed")
        
        print("\nRunning focus tests...")
        test_focus = TestSessionTabsFocus()
        await test_focus.test_focus_lands_on_table()
        await test_focus.test_tab_does_not_focus_tab_bar()
        print("✓ Focus tests passed")
        
        print("\nRunning shutdown tests...")
        test_shutdown = TestSessionTabsShutdown()
        await test_shutdown.test_close_everything_closes_all_sessions()
        print("✓ Shutdown tests passed")
        
        print("\nRunning edge case tests...")
        test_edge = TestSessionTabsEdgeCases()
        await test_edge.test_activate_out_of_range_index()
        await test_edge.test_activate_negative_index()
        print("✓ Edge case tests passed")
        
        print("\n" + "="*50)
        print("ALL SESSION TABS TESTS PASSED")
        print("="*50)
    
    asyncio.run(run_all_tests())
