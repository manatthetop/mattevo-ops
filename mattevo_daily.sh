#!/bin/bash
# MATTevo daily driver. Safe to run twice; it will not stack hunts.
# Usage:  ./mattevo_daily.sh          -> full daily run (Tor, pull, checks, hunt)
#         ./mattevo_daily.sh status   -> just read the scoreboard
set -u
REPO="$HOME/mattevo-ops"
TORRC="$HOME/.mattevo-tor/torrc"
cd "$REPO" || { echo "no repo at $REPO"; exit 1; }

if [ "${1:-}" = "status" ]; then
    echo "--- scoreboard $(date -u +%FT%TZ) ---"
    [ -f hunt.log ] && tail -3 hunt.log
    grep -o '"event": "[^"]*"' evidence/watcher.jsonl | sort | uniq -c
    grep -v '"event": "pass"\|"event": "circuit"\|"event": "selftest"\|"event": "address-watch"' evidence/watcher.jsonl | tail -3
    pgrep -f watcher.py >/dev/null && echo "hunt: RUNNING" || echo "hunt: idle (run: ./mattevo_daily.sh)"
    exit 0
fi

# 1. Tor client (own config, never a relay)
pgrep -x tor >/dev/null || nohup /usr/sbin/tor -f "$TORRC" --quiet >/dev/null 2>&1 &
for i in $(seq 1 20); do
    (exec 3<>/dev/tcp/127.0.0.1/9050) 2>/dev/null && break
    sleep 1
done
echo "[daily] tor: up"

# 2. latest code, never fight the other machine
git stash -q 2>/dev/null; git pull -q --rebase origin main; git stash pop -q 2>/dev/null

# 3. gates: key must be the real one, selftest must pass
FP=$(python3 -c "import json; print(json.load(open('ledgers/vault.json'))['fingerprint'])" 2>/dev/null)
SRC=$(python3 -c "import json; print(json.load(open('ledgers/vault.json'))['cold_key_source'])" 2>/dev/null)
echo "[daily] key: $FP from $SRC"
[ "$FP" = "60e8ad91" ] || echo "[daily] WARNING: wrong fingerprint, do NOT trust balances, see commands doc section 0"
timeout 120 python3 watcher.py --selftest >/dev/null 2>&1 \
    && echo "[daily] selftest: PASS" || echo "[daily] selftest: FAIL (tor down?)"

# 4. the hunt: one long background sweep, ~2 hours, log to hunt.log
if pgrep -f "watcher.py --sample" >/dev/null; then
    echo "[daily] hunt already RUNNING (pid $(pgrep -f 'watcher.py --sample' | head -1)); tail with: tail -f $REPO/hunt.log"
else
    : > hunt.log
    nohup bash -c 'for i in $(seq 1 8); do /usr/bin/timeout 1200 python3 watcher.py --sample 1500 >> hunt.log 2>&1; sleep 60; done; echo "HUNT OVER" >> hunt.log' >/dev/null 2>&1 &
    echo "[daily] hunt STARTED (8 passes, ~2h). Watch: tail -f $REPO/hunt.log"
fi

# 5. keep evidence committed so the other machine sees it
sleep 5
git add evidence/watcher.jsonl evidence/watched_seen.json 2>/dev/null
git commit -q -m "evidence: daily hunt run" 2>/dev/null && \
    { git stash -q 2>/dev/null; git pull -q --rebase --autostash origin main 2>/dev/null; git push -q origin main 2>/dev/null; git stash pop -q 2>/dev/null; }
echo "[daily] done. status any time: ./mattevo_daily.sh status"
