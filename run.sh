#!/bin/bash
# ================================================================
# JDL Trade — Quick launcher
# Usage:
#   bash ~/jdltrading/run.sh              # interactive REPL
#   bash ~/jdltrading/run.sh "task here"  # single-shot
#   bash ~/jdltrading/run.sh --verbose    # show thinking
#   bash ~/jdltrading/run.sh --help       # all options
# ================================================================
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 main.py "$@"
