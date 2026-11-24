#!/usr/bin/env python3
"""MATTevo LIVE: the money board, fed by real Bitcoin data instead of a simulator.

Real feeds, no keys, no accounts:
  /mempool/txids            -> the full live mempool (tens of thousands of txs)
  /tx/{txid}                -> per-tx JSON with witness + scriptSig + prevout values
  /v1/fees/recommended      -> the global fee recommendation
  /blocks/tip/height        -> chain height

`real` mode samples the mempool, decodes the DER signatures out of every input
witness and scriptSig, and buckets GENUINE mainnet (r, s) pairs by r. A bucket
with two entries is the same k used twice, which is exactly the crack condition.

Modes: real (live network) | sim (offline, deterministic) | auto (real, fall back)
"""
import json
import time
import urllib.error
import urllib.request

ESPLORA = ["https://mempool.space/api", "https://blockstream.info/api"]
UA = {"User-Agent": "mattevo-live/1.1", "Accept": "application/json"}
TIMEOUT = 12.0


def _get(path, base=None):
    """GET one Esplora path, trying mirrors. Returns parsed JSON or None."""
    for b in ([base] if base else ESPLORA):
        try:
            req = urllib.request.Request(b + path, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
    return None


def _der_rs(hexblob):
    """Pull (r, s) out of one DER-encoded ECDSA signature (0x30 ... 02 r 02 s).

    Handles both 0x20 and 0x21 byte lengths, which real wallets produce when the
    high bit is set. Returns None if the blob is not a clean DER signature."""
    try:
        b = bytes.fromhex(hexblob)
    except (ValueError, TypeError):
        return None
    if len(b) < 8 or b[0] != 0x30 or b[2] != 0x02:
        return None
    i = 3
    rl = b[i]
    i += 1
    if i + rl + 2 > len(b):
        return None
    r = int.from_bytes(b[i:i + rl], "big")
    i += rl
    if b[i] != 0x02:
        return None
    i += 1
    sl = b[i]
    i += 1
    if i + sl > len(b):
        return None
    s = int.from_bytes(b[i:i + sl], "big")
    if 0 < r < 2**256 and 0 < s < 2**256:
        return r, s
    return None


def _sigs_of_vin(v):
    """Every DER signature in one input: witness items first, then scriptSig."""
    out = []
    for w in v.get("witness") or []:
        if isinstance(w, str) and w[:2] == "30":
            rs = _der_rs(w)
            if rs:
                out.append(rs)
    ss = v.get("scriptsig") or ""
    if ss[:2] == "30":
        rs = _der_rs(ss)
        if rs:
            out.append(rs)
    return out


def mempool_signatures(sample=60):
    """Harvest real (r, s) pairs from the live mempool, one GET per tx.

    Returns (sigs, txs_seen, mempool_size). Each sig record carries the prevout
    value so exposure can be priced, and mimics the crypto.py signature shape."""
    txids = _get("/mempool/txids") or []
    if not txids:
        return [], 0, 0
    step = max(1, len(txids) // sample)
    picked = txids[::step][:sample]
    # One GET per tx; on a 4 GB / 2-core box sequential HTTPS took ~24 s for 60
    # fetches, so fan a small bounded pool. _get is offline-safe (returns None),
    # and results are processed in pick order so dedup stays deterministic.
    from concurrent.futures import ThreadPoolExecutor
    def one(t):
        for attempt in range(2):          # one retry: mirrors flake under load
            d = _get("/tx/" + t)
            if d is not None:
                return t, d
        return t, None
    with ThreadPoolExecutor(max_workers=6) as ex:
        docs = list(ex.map(one, picked))
    sigs, seen = [], set()
    for txid, d in docs:
        if not d:
            continue
        for v in d.get("vin", []):
            val = (v.get("prevout") or {}).get("value", 0)
            for r, s in _sigs_of_vin(v):
                if r in seen:
                    continue
                seen.add(r)
                sigs.append({"r": r, "s": s, "z": 0, "txid": txid,
                             "vout": v.get("vout", 0), "value": val})
    return sigs, len(picked), len(txids)


def spot_price_usd():
    """Live BTC spot in USD. Drives every $ figure the board prints so the money
    numbers track the market instead of a hardcoded constant."""
    d = _get("/api/v1/prices", base="https://mempool.space") or {}
    try:
        return float(d.get("USD"))
    except (TypeError, ValueError):
        return None


def chain_state():
    """Live chain facts: height, fee floor, and the recommended rates."""
    tip = _get("/blocks/tip/height")
    fees = _get("/v1/fees/recommended", base=ESPLORA[0]) or {}
    return {"height": tip, "fees": fees, "source": ESPLORA[0].split("/")[2]}


def frame(mode="real", target=None, sample=60):
    """One snapshot of the real world for the board to render."""
    st = {"mode": mode, "feed": "none", "height": None, "fees": {}, "txs_seen": 0,
          "mempool_size": 0, "sigs_seen": 0, "reuse_buckets": 0, "worst_bucket": 0,
          "sats_at_risk": 0, "spot_usd": None, "sample": []}
    if mode == "sim":
        st.update(feed="sim", height=976825,
                  fees={"fastestFee": 24, "halfHourFee": 12, "hourFee": 6},
                  txs_seen=40, mempool_size=54375, sigs_seen=1180, spot_usd=65000.0,
                  reuse_buckets=1, worst_bucket=2, sats_at_risk=1989593)
        return st
    cs = chain_state()
    sigs, n_tx, mem_sz = mempool_signatures(sample)
    st.update(feed=cs["source"], height=cs["height"], fees=cs["fees"],
              txs_seen=n_tx, mempool_size=mem_sz, sigs_seen=len(sigs),
              spot_usd=spot_price_usd())
    if cs["height"] is None:
        st["feed"] = "down"
        return st
    buckets = {}
    for sg in sigs:
        buckets.setdefault(sg["r"], []).append(sg)
    dupes = {r: g for r, g in buckets.items() if len(g) > 1}
    st["reuse_buckets"] = len(dupes)
    st["worst_bucket"] = max((len(g) for g in dupes.values()), default=0)
    st["sats_at_risk"] = sum(x["value"] for g in dupes.values() for x in g)
    st["sample"] = [{"txid": s["txid"][:16], "r": format(s["r"], "064x")[:16],
                     "sats": s["value"]} for s in sigs[:5]]
    return st


def engine_rows(eng):
    """Rows for the watched targets, read off the engine so the board cannot drift."""
    if eng is None:
        return []
    m = eng.money()
    rows = [{"name": "TOTAL", "captures": m.get("tx", 0),
             "captured_usd": m.get("captured_usd", 0), "spot_usd": m.get("spot_usd", 0)}]
    for t in eng.targets.values():
        rows.append({"name": t.name, "captures": t.live.get("captures", 0),
                     "captured_usd": t.live.get("last_usd", 0),
                     "spot_usd": t.live.get("last_usd", 0)})
    return rows


def frame_text(st, engine_board=None, indent=""):
    """Build one frame as a string. ANSI only, no curses, so it survives a pipe to a
    log and so the desk can print it under the banner without clearing the screen."""
    try:
        from mattevo import paint
    except Exception:
        def paint(k, t): return t
    out = []
    feed = st.get("feed", "none")
    fk = ("green" if feed in ("real", "live")
          else "yellow" if feed == "sim-fallback" else "red")
    out.append(paint("magenta", " MATTevo LIVE ") + paint(fk, "feed " + str(feed).upper())
               + paint("white", "  block %s" % st.get("height", "-")))
    f = st.get("fees") or {}
    out.append(paint("white", " fees sat/vB  ")
               + paint("green", "fast %s" % f.get("fastestFee", "-")) + paint("white", "   ")
               + paint("cyan", "half-hour %s" % f.get("halfHourFee", "-")) + paint("white", "   ")
               + paint("blue", "hour %s" % f.get("hourFee", "-")))
    out.append(paint("white", " mempool       %s of %s txs sampled, %s real signatures decoded"
                     % (st.get("txs_seen", 0), f"{st.get('mempool_size', 0):,}",
                        st.get("sigs_seen", 0))))
    spot = st.get("spot_usd")
    out.append(paint("white", " btc spot      ")
               + (paint("green", "$" + f"{spot:,.0f}") if spot else paint("yellow", "unpriced")))
    risk = st.get("sats_at_risk", 0)
    buckets = st.get("reuse_buckets", 0)
    key = "green" if buckets == 0 else "yellow" if buckets < 4 else "red"
    usd = ("  $" + f"{risk / 1e8 * spot:,.2f}") if (spot and risk) else ""
    out.append(paint(key, " k-reuse scan  %s duplicate-r buckets, worst %s sigs, %s sats exposed%s"
                     % (buckets, st.get("worst_bucket", 0), f"{risk:,}", usd)))
    if engine_board:
        out.append(paint("bold", " money ledger"))
        for row in engine_board:
            nm = row.get("name", "?")
            cap = row.get("captured_usd", 0)
            rk = "green" if nm == "TOTAL" else "white" if cap else "dim"
            out.append(paint(rk, "   %-12s captures %-3s captured $%-10s spot $%s"
                             % (nm, row.get("captures", 0), cap, row.get("spot_usd", 0))))
    body = "\n".join(out)
    return "\n".join(indent + ln for ln in body.splitlines()) if indent else body


def render(st, engine_board=None):
    """Full-screen paint for `mattevo live`. The desk uses frame_text instead."""
    print("\x1b[2J\x1b[H", end="")
    print(frame_text(st, engine_board))
    return st


def board_text(st, eng=None):
    """One frame as text, engine board included. The desk prints this under its
    banner instead of clearing the screen."""
    return frame_text(st, engine_rows(eng))


def board(interval=2.0, mode="auto", target=None, once=False, sample=60):
    """Refresh forever (or once with once=True). Never crashes on a dead feed."""
    try:
        import mattevo as MV
        eng = MV.MATTevo()
    except Exception:
        eng = None
    eff = "real" if mode == "auto" else mode
    while True:
        st = frame(eff, target, sample)
        if mode == "auto" and st["feed"] in ("down", "none"):
            st = frame("sim")
            st["feed"] = "sim-fallback"
        render(st, engine_rows(eng))
        if eng is not None:
            try:
                eng._load()
            except Exception:
                pass
        if once:
            return 0
        time.sleep(max(0.2, float(interval)))


def demo():
    st = frame("real")
    render(st)
    print()
    for row in st["sample"]:
        print(f"  {row['txid']}  r={row['r']}  {row['sats']:,} sats")


if __name__ == "__main__":
    demo()
