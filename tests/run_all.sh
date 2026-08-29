#!/usr/bin/env bash
# Run every SURFTP check. No pytest yet: these are standalone scripts that
# start their own servers and exit non-zero on the first failing suite.
set -u
cd "$(dirname "$0")/.."
PY=./tenv/bin/python
status=0
for suite in tests/test_vault.py tests/test_ui.py tests/test_sessions.py tests/test_integration.py tests/test_ftp.py tests/test_ui_connect.py; do
    echo "════ $suite"
    PYTHONPATH="$PWD" $PY "$suite" || status=1
done
echo
[ $status -eq 0 ] && echo "ALL SUITES PASSED" || echo "SOME SUITES FAILED"
exit $status
