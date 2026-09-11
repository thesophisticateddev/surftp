#!/usr/bin/env bash
# Run every SURFTP check.
#
# Two families, both required:
#   - standalone scripts, which start their own SSH/FTP servers and exit
#     non-zero on failure;
#   - pytest suites, which are unit-level and need no server.
# Both are listed here because "every suite" must actually mean every suite —
# four pytest files were previously invisible to this script and so never ran.
#
# tests/test_frozen.py is the deliberate exception: it needs built artifacts in
# dist/, so it runs after `pyinstaller packaging/surftp.spec` (see
# docs/building.md) rather than on every source change.
set -u
cd "$(dirname "$0")/.."
# The developer venv by default; CI overrides with PY=python, where the
# dependencies are installed into the runner's own interpreter.
PY="${PY:-./tenv/bin/python}"
status=0

for suite in tests/test_vault.py tests/test_migration.py tests/test_ssh_units.py tests/test_paths.py \
             tests/test_ui.py tests/test_sessions.py tests/test_integration.py tests/test_ssh_integration.py \
             tests/test_ftp.py tests/test_transfer.py tests/test_ui_connect.py tests/test_ssh_ui.py; do
    echo "════ $suite"
    PYTHONPATH="$PWD" $PY "$suite" || status=1
done

PYTEST_SUITES="tests/test_permissions.py tests/test_session_tabs.py tests/test_shell.py tests/test_shell_integration.py"
echo "════ pytest: $PYTEST_SUITES"
if $PY -c "import pytest" 2>/dev/null; then
    PYTHONPATH="$PWD" $PY -m pytest $PYTEST_SUITES -q || status=1
else
    echo "SKIPPED - pytest not installed (pip install pytest pytest-asyncio)"
    status=1
fi

echo
[ $status -eq 0 ] && echo "ALL SUITES PASSED" || echo "SOME SUITES FAILED"
exit $status
