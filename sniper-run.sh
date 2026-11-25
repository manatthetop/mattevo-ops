#!/usr/bin/env bash
# One-liner to start (or confirm) tonight's full hunt:
#   ./sniper-run.sh
# Same settings the night run uses: 24 blocks deep, 12k mempool sample,
# 4 workers (mempool.space throttles bursts), 5-min interval, nice 5.
# Log: hunt.log (JSONL mirrored to evidence/watcher.jsonl). State:
# evidence/sniper_state.json. Stop: kill $(cat .sniper.pid)
cd "$(dirname "$0")" || exit 1
exec ./sniperctl.sh
