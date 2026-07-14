#!/usr/bin/env bash
# NICo Emulator — Site-Local Control Plane (standalone) — http://127.0.0.1:9000
# Physical Vera Rubin NVL72 twin is owned by the AI Infra Emulator (:9100),
# reached over REST. Override its location with AI_INFRA_URL (default below).
cd "$(dirname "$0")"
export AI_INFRA_URL="${AI_INFRA_URL:-http://127.0.0.1:9100}"
PY="${PYTHON:-python3}"
[ -d .venv ] && PY=.venv/bin/python
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 9000 "$@"
