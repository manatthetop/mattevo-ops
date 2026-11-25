#!/usr/bin/env bash
# Night hunt driver: keep the clearnet sniper running, one log, no overlaps.
cd "$(dirname "$0")" || exit 1
LOG=hunt.log
TODAY=$(date -u +%Y-%m-%d)
PIDF=.sniper.pid

# Note the bracket: this script's own command line (bash ./sniperctl.sh)
# contains the pattern string, so an unbracketed pgrep matches IT and the
# guard falsely reports "already running" forever (happened 2026-11-25).
if pgrep -f "python3 sniper.py --loo[p]" >/dev/null; then
    echo "sniper already running:"; pgrep -af "python3 sniper.py --loo[p]"; exit 0
fi
if [ -f "$LOG" ] && ! grep -q "$TODAY" "$LOG" 2>/dev/null; then
    mv "$LOG" "evidence/hunt-$TODAY.log" 2>/dev/null || true
fi
touch "$LOG"    # logline() only mirrors to hunt.log when it already exists
# Eager imports (secp256k1 tables) take ~1 min on this 2-core box; $! is the
# pid BEFORE python replaces itself, so read the real pid back from pgrep.
nohup nice -n 5 python3 sniper.py --loop --blocks 24 --sample 12000 \
    --workers 4 --interval 300 >> "$LOG" 2>&1 &
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    RP=$(pgrep -f "python3 sniper.py --loo[p]" | head -1) && [ -n "$RP" ] && break
done
echo "${RP:-$!}" > "$PIDF"
echo "[sniperctl] $TODAY start: blocks=24 mempool-sample=12000 interval=300 pid=$(cat $PIDF)" >> "$LOG"
echo "sniperctl: started pid $(cat $PIDF), logging to $LOG"
