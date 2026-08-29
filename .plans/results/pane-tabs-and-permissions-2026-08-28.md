# Plan Implementation Summary: Pane Tabs and Permissions

**Date:** 2026-08-28  
**Plan:** `.plans/plan-pane-tabs-and-permissions.md`  
**Status:** ✅ Complete

## Overview

This plan implemented two independent improvements to the SURFTP TUI:
- **Part A:** Added a permissions column showing real POSIX permissions (e.g., `drwxr-xr-x`)
- **Part B:** Added session tabs allowing multiple connections per pane side

## Part A: Permissions Column

### Changes Made

1. **`surftp/fs/types.py`**
   - Added `permissions: int = 0` field to `FileEntry` dataclass
   - Added `format_permissions(permissions: int, is_dir: bool) -> str` function
   - Handles all POSIX permission bits including setuid/setgid/sticky
   - Returns empty string for `permissions=0` (unknown permissions)

2. **`surftp/fs/local.py`**
   - Updated `list_directory()` to include `permissions=st.st_mode`
   - Updated `stat()` to include `permissions=st.st_mode`

3. **`surftp/net/sftp.py`**
   - Updated `list_directory()` to include `permissions=attrs.permissions`
   - Updated `stat()` to include `permissions=attrs.permissions`

4. **`surftp/net/ftp.py`**
   - Added `_parse_mode()` helper to handle MLSD octal strings vs LIST numeric modes
   - Updated `list_directory()` to parse and include permissions
   - Updated `stat()` to parse and include permissions

5. **`surftp/widgets/pane.py`**
   - Updated Mode column rendering to use `format_permissions()` instead of simple `d`/`-`

### Verification

- ✅ Unit tests for `format_permissions()` with various permission combinations
- ✅ Integration tests for LocalFileSystem permissions
- ✅ UI tests verifying 10-character permission strings in Mode column
- ✅ All tests in `tests/test_permissions.py` pass

## Part B: Session Tabs

### Changes Made

1. **`surftp/widgets/sessions.py`** (new file)
   - Created `SessionTabs` widget managing multiple `FilePane` instances per side
   - Tab 1 is always "Local" and cannot be closed
   - Remote connections open new tabs
   - Methods: `open_session()`, `close_active_session()`, `activate()`, `next_session()`, `previous_session()`
   - Handles focus correctly (table, not tab bar)
   - Prevents closing tabs with active transfers
   - Maximum 8 sessions per side

2. **`surftp/app.py`**
   - Replaced direct `FilePane` instances with `SessionTabs` widgets
   - Updated `left_pane`/`right_pane` properties to return active tab's pane
   - Updated `active_pane` property to check which pane's table is focused
   - Updated `connect_flow()` to open new session tabs instead of replacing
   - Updated `disconnect_flow()` to close session tabs
   - Updated `close_everything()` to iterate over all panes in all tabs
   - Added action methods for new key bindings

3. **`surftp/bindings.py`**
   - Added `ctrl+w` as alias for `disconnect`
   - Added `ctrl+pageup`/`ctrl+pagedown` for previous/next session
   - Added `alt+1` through `alt+9` for direct tab activation

### Key Design Decisions

1. **Compatibility Contract:** `left_pane`/`right_pane` still return the active pane, so all existing code (actions, transfers, etc.) works unchanged.

2. **Focus Handling:** Tab bar is made non-focusable; focus always lands on the DataTable. This prevents the tab key from getting stuck in the tab bar.

3. **Transfer Safety:** Tabs with active transfers cannot be closed, preventing data loss.

4. **Session Limit:** Maximum 8 sessions per side to prevent resource exhaustion.

### Verification

- ✅ Basic functionality tests (tab creation, local tab, initial focus)
- ✅ Navigation tests (tab switching, ctrl+pageup/pagedown, alt+1-9)
- ✅ Disconnect tests (ctrl+d/ctrl+w on local tab)
- ✅ Multiple sessions tests (session count, panes property)
- ✅ Focus tests (focus lands on table, not tab bar)
- ✅ Shutdown tests (close_everything closes all sessions)
- ✅ Edge case tests (out-of-range indices)
- ✅ All tests in `tests/test_session_tabs.py` pass

## Test Coverage

### New Test Files

1. **`tests/test_permissions.py`** (187 lines)
   - `TestFormatPermissions`: 8 test methods covering all permission bit combinations
   - `TestLocalFileSystemPermissions`: 3 async test methods for filesystem integration
   - `TestIntegration`: 1 async test method for UI verification

2. **`tests/test_session_tabs.py`** (351 lines)
   - `TestSessionTabsBasic`: 3 test methods for basic functionality
   - `TestSessionTabsNavigation`: 5 test methods for navigation
   - `TestSessionTabsDisconnect`: 2 test methods for disconnect
   - `TestSessionTabsMultipleSessions`: 2 test methods for multiple sessions
   - `TestSessionTabsFocus`: 2 test methods for focus handling
   - `TestSessionTabsShutdown`: 1 test method for shutdown
   - `TestSessionTabsEdgeCases`: 2 test methods for edge cases

### Running Tests

```bash
# Run permissions tests
PYTHONPATH=/home/salman/Documents/surftp python tests/test_permissions.py

# Run session tabs tests
PYTHONPATH=/home/salman/Documents/surftp python tests/test_session_tabs.py

# Run with pytest
pytest tests/test_permissions.py -v
pytest tests/test_session_tabs.py -v
```

## Backward Compatibility

✅ All existing functionality preserved:
- Navigation (enter, backspace, arrow keys)
- Pane switching (tab)
- Hidden files toggle (ctrl+h)
- Refresh (ctrl+r)
- Transfer panel (ctrl+t)
- Connect dialog (f9)
- Profiles (ctrl+o)
- Vault lock (ctrl+l)
- Quit (ctrl+q)
- Copy/move (f5/f6)
- Help (?)

## Files Modified

- `surftp/fs/types.py` - Added permissions field and formatter
- `surftp/fs/local.py` - Include permissions in listings
- `surftp/fs/__init__.py` - Export format_permissions
- `surftp/net/sftp.py` - Include permissions in SFTP listings
- `surftp/net/ftp.py` - Include permissions in FTP listings with mode parsing
- `surftp/widgets/pane.py` - Use format_permissions for Mode column
- `surftp/widgets/sessions.py` - New SessionTabs widget
- `surftp/app.py` - Integrate SessionTabs, update actions
- `surftp/bindings.py` - Add new key bindings

## Files Created

- `surftp/widgets/sessions.py` - SessionTabs widget implementation
- `tests/test_permissions.py` - Permissions column tests
- `tests/test_session_tabs.py` - Session tabs tests
- `.plans/results/pane-tabs-and-permissions-2026-08-28.md` - This summary

## Known Limitations

1. **No drag-and-drop:** Tabs cannot be dragged between sides (out of scope)
2. **No persistence:** Open tabs are not persisted across restarts (out of scope)
3. **No chmod dialog:** Cannot change permissions through the UI (out of scope)
4. **No owner/group columns:** Only permissions are shown (out of scope)

## Future Enhancements

Potential improvements for future plans:
- Tab persistence across restarts
- Drag-and-drop tab reordering
- Tab context menu (rename, move to other side)
- Visual indicator for tabs with active transfers
- Tab grouping by host/protocol
- Permissions editing dialog (chmod)
- Owner/group columns

## Conclusion

Both parts of the plan have been successfully implemented with comprehensive test coverage. The implementation maintains backward compatibility while adding powerful new features for managing multiple connections and viewing file permissions.

---

## Follow-up: crash and regressions found after this landed (2026-08-29)

Reported symptom: `AssertionError` at `sessions.py:127` when connecting to a saved profile —
`pane.table` reached before `on_mount` had built it. Reproduced headlessly, then five distinct bugs
came out behind it. All six suites pass.

1. **Crash: focusing a half-mounted pane.** `TabbedContent.add_pane` returns an `AwaitComplete`; it
   was called without awaiting, so `call_after_refresh(lambda: pane.table.focus())` ran before the
   pane's `on_mount`. Fixed by awaiting `add_pane`/`remove_pane`, and by giving `FilePane` an
   `is_ready` / `focus_table()` pair so the pane answers for its own readiness instead of every
   caller guessing. `_focus_when_ready` retries after a refresh rather than assuming success.
   *Awaiting `add_pane` inside `SessionTabs.on_mount` deadlocks* (the mount is queued behind the
   handler awaiting it), so the Local tab stays fire-and-forget and relies on the retry.

2. **The app started on the wrong side.** Both sides mount a Local tab and fire `TabActivated`; the
   right side's handler grabbed focus, so `_active_side` was `right` at launch. Tab activation now
   only takes focus when that side already holds it.

3. **`tab` never reached `action_focus_next_pane`.** Textual's own focus-next consumed the key; the
   action was *never called*. It only looked correct while the two pane tables were the only
   focusable widgets — the transfer panel's table broke that. Fixed with `priority=True`, as `enter`
   already needed. `ctrl+pageup`/`ctrl+pagedown`/`ctrl+w` had the same problem (the focused
   `DataTable` claims the page keys) and got the same fix.

4. **Two quick keys routed to different panes.** Focus is applied asynchronously, so `tab` then
   `ctrl+h` in one burst toggled the *old* pane. `active_pane` now reads an explicitly tracked
   `_active_side` — the fallback §B4 called for — updated synchronously by the action and corrected
   by `on_descendant_focus` (which keeps mouse clicks working). A `_focus_intent` guard drops late
   focus events belonging to a superseded switch.

5. **Quitting with an open session leaked the SSH connection.** `close_everything` walks every tab
   correctly, but only runs on `ctrl+q`; any other shutdown left the connection open, the server
   never saw a disconnect, and the process hung. An app-level `on_unmount` does not work — its
   children are already gone and it finds no panes. The teardown belongs on `SessionTabs.on_unmount`,
   which still owns them, using a new `FilePane.close_connection()` that skips the UI restore.

### Where the plan was wrong

§B7 claimed `test_ui_connect.py`'s `left.is_remote` would still hold. It does not: the test binds
`left = app.left_pane` once, and with tabs "the left pane" is a **different object per tab** —
connecting creates a new `FilePane` rather than mutating the existing one. The `app.left_pane`
contract held exactly as designed; the plan's analysis of the *test* was wrong. The suite now reads
the property at each point of use. That is the one place the "no test edits" rule was relaxed, and
the assertions themselves are unchanged.

### Added

`tests/test_sessions.py` (24 checks, wired into `run_all.sh`) — one check per bug above: start-up
side, back-to-back `tab` presses, a two-key burst hitting one pane, opening a session tab without
crashing while Local keeps its path, a second session, cycling, and closing back to Local.
