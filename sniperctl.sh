#!/usr/bin/env bash
# Night hunt driver: keep the clearnet sniper running, one log, no overlaps.
cd "$(dirname "$0")" || exit 1
LOG=hunt.log
TODAY=$(date -u +%Y-%m-%d)
PIDF=.sniper.pid

if pgrep -f "python3 sniper.py --loop" >/dev/null; then
    echo "sniper already running:"; pgrep -af "python3 sniper.py --loop"; exit 0
fi
if [ -f "$LOG" ] && ! grep -q "$TODAY" "$LOG" 2>/dev/null; then
    mv "$LOG" "evidence/hunt-$TODAY.log" 2>/dev/null || true
fi
touch "$LOG"    # logline() only mirrors to hunt.log when it already exists
echo "[sniperctl] $TODAY start: blocks=24 mempool-sample=12000 interval=300" >> "$LOG"
nohup nice -n 5 python3 sniper.py --loop --blocks 24 --sample 12000 \
    --workers 4 --interval 300 >> "$LOG" 2>&1 &
echo $! > "$PIDF"
echo "sniperctl: started pid $(cat $PIDF), logging to $LOG"
