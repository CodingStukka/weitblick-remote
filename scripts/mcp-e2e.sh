#!/usr/bin/env bash
# MCP end-to-end test of the agent control (M7) against the test VM (credentials in .testvm.env).
#
#   scripts/mcp-e2e.sh [--takeover] [--no-build] [--runs N] [--claude]     (default: 3 runs)
#
# Goes through the real chain: scripts/mcp-e2e.py speaks MCP to the bundled weitblick-mcp helper,
# which starts a hidden copy of the app (own bundle id, temporary store, socket and log, DEBUG
# credentials and scripted permission answers) and relays to its socket. Steps: initialize and
# tools/list; Never / missing password / unknown certificate refused; connect, screenshot not
# blank; win+r notepad enter; type_text with umlauts and symbols found by find_text; ctrl+a ctrl+c
# clipboard_get; clipboard_set ctrl+v round trip; Notepad closed without saving; disconnect; the
# permission prompt (Deny, Allow once, an expiring 10-minute grant, no answer); audit log; the
# hidden app quits by itself; master switch off = no socket and no app. --claude also asks Claude
# Code (haiku) to use the tools. Evidence goes to build/evidence/m7-mcp-e2e-<time>/.
# Never touches Marc's data: the copy has its own defaults domain and store; nothing goes through
# LaunchServices, and no general pasteboard is used.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/testvm.sh"
hold_vm_lock "$0" "$@"
TAKEOVER=0
BUILD=1
RUNS=3
CLAUDE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --takeover) TAKEOVER=1 ;;
    --no-build) BUILD=0 ;;
    --runs) RUNS="$2"; shift ;;
    --claude) CLAUDE=1 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

[[ -f "$ROOT/.testvm.env" ]] || { echo "MCP-E2E FAIL: $ROOT/.testvm.env missing" >&2; exit 1; }
# Only the host and user name are read here; the password stays in the file (the app's DEBUG hook reads it).
VM_HOST="$(grep '^WEITBLICK_TEST_HOST=' "$ROOT/.testvm.env" | cut -d= -f2-)"
VM_USER="$(grep '^WEITBLICK_TEST_USER=' "$ROOT/.testvm.env" | cut -d= -f2-)"
[[ "$VM_HOST" == 10.211.55.* ]] || { echo "MCP-E2E FAIL: test host $VM_HOST is not on the test VM network" >&2; exit 1; }

EVIDENCE="$ROOT/build/evidence/m7-mcp-e2e-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$EVIDENCE"
require_free_console "$TAKEOVER" MCP-E2E

if [[ "$BUILD" == 1 ]]; then
  "$ROOT/scripts/build.sh" >"$ROOT/build/build.log" 2>&1 || { echo "MCP-E2E FAIL: build failed, see build/build.log" >&2; exit 1; }
fi

TEST_ID="nrw.neuhaus.weitblick-remote.m7test"
# Short path: Unix socket paths are limited to 103 bytes.
RUN_DIR="$(mktemp -d "$ROOT/build/m7e2e.XXXXXX")"
APP="$RUN_DIR/Weitblick Remote M7.app"
cleanup() {
  if [[ -f "$RUN_DIR/app.pid" ]]; then kill -TERM "$(cat "$RUN_DIR/app.pid")" 2>/dev/null || true; sleep 2; fi
  pkill -TERM -f "$RUN_DIR/" 2>/dev/null || true
  defaults delete "$TEST_ID" >/dev/null 2>&1 || true
  rm -rf "$RUN_DIR"
  notepad_clean_slate
}

# Notepad keeps tabs across sessions; start and end without any.
notepad_clean_slate() {
  prlctl exec "$VM_NAME" powershell -NoProfile -Command "
    Get-Process notepad -ErrorAction SilentlyContinue | Stop-Process -Force
    \$state = 'C:\\Users\\$VM_USER\\AppData\\Local\\Packages\\Microsoft.WindowsNotepad_8wekyb3d8bbwe\\LocalState'
    Remove-Item -Recurse -Force \"\$state\\TabState\", \"\$state\\WindowState\" -ErrorAction SilentlyContinue
  " >/dev/null 2>&1 || true
}
trap cleanup EXIT

# A copy with its own bundle id (own defaults domain, never Marc's), ad hoc signed without the
# hardened runtime (an ad-hoc hardened binary can't load the Debug build's debug.dylib).
cp -R "$ROOT/build/Weitblick Remote.app" "$APP"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $TEST_ID" "$APP/Contents/Info.plist"
codesign --force --deep --sign - "$APP" 2>/dev/null
defaults write "$TEST_ID" agentControlEnabled -bool YES
defaults write "$TEST_ID" confirmCloseSession -bool NO

FINGERPRINT="$(prlctl exec "$VM_NAME" powershell -NoProfile -Command \
  "Get-ChildItem 'Cert:\\LocalMachine\\Remote Desktop' | ForEach-Object { [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash(\$_.RawData)) }" \
  | tr -d '\r' | head -1)"
[[ "$FINGERPRINT" =~ ^[0-9A-F-]{95}$ ]] || { echo "MCP-E2E FAIL: no RDP certificate fingerprint from the VM ($FINGERPRINT)" >&2; exit 1; }

# The test store: the VM under several agent access settings (all on the test VM network).
VM_HOST="$VM_HOST" VM_USER="$VM_USER" FINGERPRINT="$FINGERPRINT" /usr/bin/python3 - "$RUN_DIR/connections.json" <<'EOF'
import json, os, sys, uuid
def connection(name, access, user=os.environ["VM_USER"], trusted=True):
    return {
        "schemaVersion": 1, "id": str(uuid.uuid4()).upper(), "name": name, "host": os.environ["VM_HOST"],
        "port": 3389, "username": user, "domain": "", "protocol": "rdp",
        "display": {"dynamicResolution": True, "matchScreenResolution": True, "retina": False, "fixedWidth": 1280,
                    "fixedHeight": 800, "startFullscreen": False, "useAllMonitors": False, "monitorCount": 1},
        "redirection": {"clipboard": True, "audioPlayback": "off", "microphone": False, "printers": False,
                        "driveRedirection": False, "drives": []},
        "keyboard": {"layoutOverride": "00000407"},
        "security": {"trustedCertificateFingerprints": [os.environ["FINGERPRINT"]] if trusted else [],
                     "agentAccess": access},
        "advanced": {"wakeOnLANMACAddresses": []}, "tags": [], "notes": "",
    }
store = {"schemaVersion": 1, "connections": [
    connection("Agent VM", "always"), connection("Ask VM", "ask"), connection("Never VM", "never"),
    connection("Untrusted VM", "always", trusted=False), connection("No password VM", "always", user=""),
]}
json.dump(store, open(sys.argv[1], "w"), indent=2)
EOF

# The app as weitblick-mcp starts it (WEITBLICK_MCP_APP): test store, socket and log, DEBUG hooks.
make_wrapper() {
  local prompts="$1"
  cat >"$RUN_DIR/app.sh" <<EOF
#!/bin/sh
echo \$\$ > "$RUN_DIR/app.pid"
export WEITBLICK_TESTVM_ENV="$ROOT/.testvm.env"
exec "$APP/Contents/MacOS/Weitblick Remote" "\$@" --store "$RUN_DIR/connections.json" \\
  --agent-log "$RUN_DIR/agent.log" --jump-dir "$RUN_DIR/no-jump" --jump-profile "$RUN_DIR/no-profile.plist" \\
  --debug-testvm-credentials --debug-agent-prompt "$prompts" \\
  --debug-agent-grant-seconds 8 --debug-agent-prompt-timeout 6
EOF
  chmod +x "$RUN_DIR/app.sh"
}
cat >"$RUN_DIR/helper.env" <<EOF
WEITBLICK_MCP_SOCKET=$RUN_DIR/mcp.sock
WEITBLICK_MCP_APP=$RUN_DIR/app.sh
M7_AGENT_LOG=$RUN_DIR/agent.log
EOF

FAILED=0
notepad_clean_slate
for ((run = 1; run <= RUNS; run++)); do
  echo "== run $run/$RUNS"
  # Deny, Allow once, 10 minutes, Deny after expiry; then nobody answers (timeout case).
  make_wrapper "deny,once,ten-minutes,deny"
  rm -f "$RUN_DIR/agent.log" "$RUN_DIR/app.pid"
  EXPECT_QUIT=()
  [[ "$run" == "$RUNS" ]] && EXPECT_QUIT=(--expect-quit "$RUN_DIR/app.pid")
  if ! M7_AGENT_LOG="$RUN_DIR/agent.log" /usr/bin/python3 "$ROOT/scripts/mcp-e2e.py" \
      --helper "$APP/Contents/MacOS/weitblick-mcp" --env-file "$RUN_DIR/helper.env" --out "$EVIDENCE" \
      --run "$run" --grant-seconds 8 --prompt-timeout 6 "${EXPECT_QUIT[@]}" 2>&1 | tee -a "$EVIDENCE/e2e.log"; then
    FAILED=1
  fi
  [[ "${PIPESTATUS[0]}" == 0 ]] || FAILED=1
  cp "$RUN_DIR/agent.log" "$EVIDENCE/agent-run$run.log" 2>/dev/null || true
  if [[ -f "$RUN_DIR/app.pid" ]] && kill -0 "$(cat "$RUN_DIR/app.pid")" 2>/dev/null; then
    kill -TERM "$(cat "$RUN_DIR/app.pid")"
    for _ in $(seq 1 20); do kill -0 "$(cat "$RUN_DIR/app.pid")" 2>/dev/null || break; sleep 0.5; done
  fi
  [[ -S "$RUN_DIR/mcp.sock" ]] && { echo "  FAIL socket left behind after the app quit" | tee -a "$EVIDENCE/e2e.log"; FAILED=1; }
  notepad_clean_slate
done

echo "== master switch off" | tee -a "$EVIDENCE/e2e.log"
defaults write "$TEST_ID" agentControlEnabled -bool NO
make_wrapper ""
# A socket left by a failed run was reported above; these checks start clean.
rm -f "$RUN_DIR/app.pid" "$RUN_DIR/mcp.sock"
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mcp-e2e","version":"off"}}}'
# a) the helper on its own: reads the switch from the app's defaults domain, starts nothing.
OFF_A="$(echo "$init" | WEITBLICK_MCP_SOCKET="$RUN_DIR/mcp.sock" "$APP/Contents/MacOS/weitblick-mcp" 2>/dev/null || true)"
if [[ "$OFF_A" == *"turned off"* && ! -e "$RUN_DIR/mcp.sock" && ! -f "$RUN_DIR/app.pid" ]]; then
  echo "  ok   helper alone: \"turned off\" error, no app started, no socket" | tee -a "$EVIDENCE/e2e.log"
else
  echo "  FAIL helper alone: $OFF_A" | tee -a "$EVIDENCE/e2e.log"; FAILED=1
fi
# b) through the test app: it starts, sees the switch off, opens no socket and quits.
OFF_B="$(echo "$init" | WEITBLICK_MCP_SOCKET="$RUN_DIR/mcp.sock" WEITBLICK_MCP_APP="$RUN_DIR/app.sh" \
  "$APP/Contents/MacOS/weitblick-mcp" 2>/dev/null || true)"
sleep 1
if [[ "$OFF_B" == *"turned off"* && ! -e "$RUN_DIR/mcp.sock" && -f "$RUN_DIR/app.pid" ]] \
    && ! kill -0 "$(cat "$RUN_DIR/app.pid")" 2>/dev/null; then
  echo "  ok   app with the switch off: no socket, quit at once, helper reports \"turned off\"" | tee -a "$EVIDENCE/e2e.log"
else
  echo "  FAIL app with the switch off: $OFF_B" | tee -a "$EVIDENCE/e2e.log"; FAILED=1
fi

if [[ "$CLAUDE" == 1 ]]; then
  echo "== Claude Code (haiku) as the MCP client" | tee -a "$EVIDENCE/e2e.log"
  defaults write "$TEST_ID" agentControlEnabled -bool YES
  make_wrapper ""
  rm -f "$RUN_DIR/agent.log" "$RUN_DIR/app.pid"
  /usr/bin/python3 - "$RUN_DIR/claude-mcp.json" "$APP/Contents/MacOS/weitblick-mcp" "$RUN_DIR" <<'EOF'
import json, sys
config, helper, run = sys.argv[1:]
json.dump({"mcpServers": {"weitblick": {"type": "stdio", "command": helper, "args": [], "env": {
    "WEITBLICK_MCP_SOCKET": f"{run}/mcp.sock", "WEITBLICK_MCP_APP": f"{run}/app.sh"}}}}, open(config, "w"), indent=2)
EOF
  cp "$RUN_DIR/claude-mcp.json" "$EVIDENCE/claude-mcp.json"
  set +e
  # The prompt right after -p: --allowedTools takes every following word as a tool name.
  (cd "$RUN_DIR" && claude -p "Use the weitblick MCP tools: connect to the connection named \"Agent VM\", take one screenshot, then disconnect. Your final answer must describe in two sentences what was visible on the Windows screen." \
    --strict-mcp-config --mcp-config "$RUN_DIR/claude-mcp.json" --model haiku --allowedTools "mcp__weitblick") \
    >"$EVIDENCE/claude.txt" 2>"$EVIDENCE/claude.stderr"
  CLAUDE_STATUS=$?
  set -e
  cat "$EVIDENCE/claude.txt" | tee -a "$EVIDENCE/e2e.log"
  cp "$RUN_DIR/agent.log" "$EVIDENCE/agent-claude.log" 2>/dev/null || true
  if [[ "$CLAUDE_STATUS" == 0 ]] && grep -q 'tool=screenshot' "$EVIDENCE/agent-claude.log" 2>/dev/null \
      && grep -q 'tool=connect' "$EVIDENCE/agent-claude.log" \
      && grep -qiE 'desktop|taskbar|icon|window' "$EVIDENCE/claude.txt"; then
    echo "  ok   Claude Code connected, took a screenshot (audit log) and described the screen" | tee -a "$EVIDENCE/e2e.log"
  else
    echo "  FAIL Claude Code check (status $CLAUDE_STATUS, see claude.stderr / agent-claude.log)" | tee -a "$EVIDENCE/e2e.log"
    FAILED=1
  fi
fi

echo "evidence: $EVIDENCE"
if [[ "$FAILED" == 0 ]]; then echo "MCP-E2E PASS"; else echo "MCP-E2E FAIL"; fi
exit "$FAILED"
