#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "$0")/service.sh"

port=${PORT:-8765}
prepare_start

python=.venv/bin/python
if [[ ! -x "$python" ]]; then
    python3 -m venv .venv
fi
"$python" -c 'import sys
if sys.version_info < (3, 11):
    sys.exit("后端需要 Python 3.11 或更高版本，请用该版本创建 .venv。")'
"$python" -m pip --disable-pip-version-check install --quiet -r requirements.txt

launch_service "$python" -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "$port"
