#!/bin/bash
set -euo pipefail
mkdir -p .unitares
cat > .unitares/session-01a0b640-9040-72f2-8e06-dcddbe8b23d6.json <<'JSON'
{"server_url": "http://localhost:8767", "agent_name": "claude", "slot": "01a0b640-9040-72f2-8e06-dcddbe8b23d6", "uuid": "37391aae-393a-43bf-b05d-5583a949e427", "agent_id": "claude-app#37391aae", "client_session_id": "01a0b640-9040-72f2-8e06-dcddbe8b23d6", "session_resolution_source": "onboard", "display_name": "claude-app", "updated_at": "2026-09-19T08:12:00Z"}
JSON
