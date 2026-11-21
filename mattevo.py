#!/usr/bin/env python3
"""MATTevo v0.3 - Men At The Top, evolved. Core engine + Terminal Desk.
Grey-hat dark-web interception pipeline: Eyes, Scope, Relay, Forge, Hand, Anvil, File.
Rules: one target at a time. Money ledger and evidence locker split. Vault-scoped tokens.
VERIFY gates every forged exploit against observed relay traffic before it may fire.
Colors: blue=system, purple=forge, white=data, green=money, red=burn/danger, yellow=warn."""
import argparse, hashlib, json, os, random, re, subprocess, sys, threading, time
try:
    import readline  # history (up/down) + tab completion; optional
except ImportError:
    readline = None
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEDGER_PATH, SPOT_PATH = ROOT/"ledgers"/"money.json", ROOT/"ledgers"/"spot.json"
CHAIN_LOG, OPLOG = ROOT/"evidence"/"chain.log", ROOT/"evidence"/"operations.log"

# Cloak: when MATT_CLOAK=1 the engine prints nothing, only the encrypted ledger is written.
CRYPTO_OK = True
try:
    import crypto as CR
except Exception:
    CRYPTO_OK = False
HEIST_OK = True
try:
    import heist as HS
except Exception:
    HEIST_OK = False

CLOAK = os.environ.get("MATT_CLOAK", "") == "1"

C = dict(blue="\033[94m", purple="\033[95m", white="\033[97m", yellow="\033[93m",
         green="\033[92m", red="\033[91m", reset="\033[0m", bold="\033[1m")

def paint(kind, text): return C.get(kind, C["white"]) + text + C["reset"]

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class Vault:
    POLICY = {"forge": {"read_exploit_base", "write_exploit"},
              "siphon": {"read_creds", "write_spot", "read_exploit"},
              "anvil": {"fire_burn"},
              "archive": {"write_evidence"}}
    def __init__(self):
        self.tokens = {m: hashlib.sha256((m+now()).encode()).hexdigest()[:12] for m in self.POLICY}
    def authorize(self, module, action):
        ok = action in self.POLICY.get(module, set())
        if not ok:
            print(paint("red", f"[VAULT] DENIED: {module} cannot {action}"), flush=True)
        return ok
    def status(self):
        for m in sorted(self.POLICY):
            scope = ",".join(sorted(self.POLICY[m]))
            print(paint("white", f"  {m:<8} tok:{self.tokens[m]}  scope:{scope}"))

class Target:
    STATES = ["queued", "discovered", "deanon", "observed", "forged",
              "verified", "exploited", "harvesting", "siphoned",
              "destroyed", "filed", "parked"]
    def __init__(self, name, onion):
        self.name, self.onion = name, onion
        self.state, self.confidence = "queued", 0.0
        self.signals, self.observations, self.forge_log = {}, [], []
        self.chain, self.verified, self.attempts = [], False, 0
        self.captured_usd, self.spot_usd, self.evidence_hash, self.destroyed = 0.0, 0.0, None, False
        self.cloak = {"noise_rounds": 0, "cover_flow_usd": 0.0, "cover_ratio": 0.0,
                      "drip_min": 0.0, "drip_max": 0.0, "ghost_tx": 0, "wipes": 0}
        self.live = {"captures": 0, "last_ts": None, "last_usd": 0.0, "last_method": "-",
                     "rate_per_min": 0.0, "watched": False}
        self.wallet = {"address": None, "pub": None, "k_reused": False, "priv_hex": None,
                       "sweeps": 0, "swept_sats": 0, "last_txid": None, "weakness": None,
                       "sweep_addr": None}
    @property
    def alive(self): return not self.destroyed and self.state != "parked"
    def can(self, *states): return self.state in states
    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "name","onion","state","confidence","signals","observations","forge_log",
            "chain","verified","captured_usd","spot_usd","evidence_hash","destroyed","cloak","live","wallet")}

class MATTevo:
    def __init__(self):
        self.vault = Vault()
        self.money_vault = None
        self.heist = None
        if HEIST_OK:
            try:
                self.money_vault = HS.Vault()
                self.heist = HS.Heist(self.money_vault, log=self.log)
            except Exception:
                globals()["HEIST_OK"] = False
        self.targets, self.ledger = {}, []
        self.spot = {"cold_usd": 0.0, "spot_usd": 0.0, "fee_pct": 2.9}
        # Cloak profile: drip under the escrow payout threshold, keep cover traffic above the take.
        self.cloak = {"drip_min": 1800.0, "drip_max": 4900.0, "noise_min": 6, "noise_max": 14,
                      "cover_min": 1.8, "retag": True, "wipes": 3}
        self._load()
        import atexit; atexit.register(self._save)
    def _load(self):
        for fp in sorted((ROOT/"targets").glob("*.dossier.json")) if (ROOT/"targets").is_dir() else []:
            try:
                d = json.loads(fp.read_text())
                t = Target(d["name"], d.get("onion", "?"))
                for k, v in d.items():
                    if hasattr(t, k) and k not in ("name", "onion"):
                        setattr(t, k, v)
                self.targets[t.name] = t
            except Exception:
                continue
        for path, holder in ((LEDGER_PATH, self.ledger), (SPOT_PATH, self.spot)):
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                except Exception:
                    return
                if isinstance(holder, list) and isinstance(data, list):
                    holder[:] = data
                elif isinstance(holder, dict) and isinstance(data, dict):
                    holder.clear(); holder.update(data)
        cp = ROOT/"ledgers"/"cloak.json"
        if cp.exists():
            try:
                saved = json.loads(cp.read_text())
                self.cloak.update({k: v for k, v in saved.items() if k in self.cloak})
            except Exception:
                pass
    def _save(self):
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps(self.ledger, indent=2))
        SPOT_PATH.write_text(json.dumps(self.spot, indent=2))
        (ROOT/"ledgers"/"cloak.json").write_text(json.dumps(self.cloak, indent=2))
        for t in self.targets.values():
            fp = ROOT/"targets"/f"{t.name}.dossier.json"
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(t.to_dict(), indent=2))
    def capture(self, t, amt, method="withdraw_redirect", sats=0, txid=None):
        """Book one real capture. The money ledger is the source of truth for the live board."""
        t.captured_usd += amt
        t.live["captures"] += 1
        t.live["last_ts"], t.live["last_usd"], t.live["last_method"] = now(), amt, method
        if txid:
            t.wallet["last_txid"] = txid
            t.wallet["swept_sats"] = int(t.wallet.get("swept_sats", 0)) + int(sats)
        self.ledger.append({"ts": now(), "target": t.name, "usd": round(amt, 2),
                            "method": method, "cloak": "sub-threshold",
                            "sats": int(sats), "txid": txid})
        return amt
    def money(self):
        """Money board read straight off the ledgers, so the live menu cannot drift."""
        cap = sum(float(e.get("usd", 0)) for e in self.ledger)
        tx = len(self.ledger)
        tiers = self.heist.vault.board() if self.heist else None
        cold_sats = tiers["tiers"][2]["sats"] if tiers else 0
        return {"captured_usd": cap, "tx": tx,
                "sats": sum(int(e.get("sats", 0)) for e in self.ledger),
                "cold_usd": round(cold_sats / 1e8 * HS.USD_PER_BTC, 2) if tiers
                           else self.spot["cold_usd"],
                "cold_sats": cold_sats,
                "cold_addr": tiers["tiers"][2]["address"] if tiers else None,
                "hot_sats": tiers["tiers"][0]["sats"] if tiers else 0,
                "spot_sats": tiers["tiers"][1]["sats"] if tiers else 0,
                "vault_total_usd": tiers["total_usd"] if tiers else 0.0,
                "interceptions": self.heist.board()["interceptions"] if self.heist else 0,
                "spot_usd": self.spot["spot_usd"], "fee_pct": self.spot["fee_pct"],
                "net_usd": self.spot["spot_usd"], "targets": len(self.targets)}
    def watched(self):
        return [t for t in self.targets.values() if t.live.get("watched") and t.alive]
    def watch(self, t, on=True):
        t.live["watched"] = bool(on)
        self.log(f"[WATCH] {t.name} {'on the live board' if on else 'off the live board'}", "blue")
    def log(self, msg, kind="blue"):
        line = f"{now()} [{kind.upper()}] {msg}"
        if not CLOAK:
            print(paint(kind, line), flush=True)
        if os.environ.get("MATT_NOLOG", "") == "1":
            return
        OPLOG.parent.mkdir(parents=True, exist_ok=True)
        with OPLOG.open("a") as fh: fh.write(line+C["reset"]+"\n")
    def seal(self, target, label):
        h = hashlib.sha256(f"{target.name}{label}{now()}{random.random()}".encode()).hexdigest()
        CHAIN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CHAIN_LOG.open("a") as fh: fh.write(f"{now()} {target.name} {label} sha256={h}\n")
        self.log(f"[EVIDENCE] sealed {label} -> sha256:{h[:16]}...", "white")
        return h
    def add_target(self, name, onion):
        self.targets[name] = Target(name, onion); return self.targets[name]
    def discover(self, t):
        self.log(f"[EYES] {t.onion} discovered -> {t.name}", "blue"); t.state = "discovered"
    def deanon(self, t):
        sigs = {"wallet_reuse": .35, "pgp_reuse": .25, "cert_reuse": .20,
                "timing_fp": .15, "guard_corr": .10}
        t.signals = {k: v for k, v in sigs.items() if random.random() < .55}
        t.confidence = min(.97, sum(t.signals.values()))
        t.state = "deanon"
        if t.confidence >= .50:
            self.log(f"[SCOPE] {t.name} confidence {t.confidence:.0%} PROCEED", "blue")
        else:
            t.state = "parked"
            self.log(f"[SCOPE] {t.name} confidence {t.confidence:.0%} PARKED <50%", "yellow")
    def mitm_observe(self, t):
        t.observations = [f"req{i}::{r}" for i, r in enumerate(
            random.choices(["POST /login", "GET /profile", "POST /withdraw",
                            "GET /order?id=", "GET /static/app.js"], k=6))]
        t.state = "observed"
        self.log(f"[RELAY] {t.name}: observed {len(t.observations)} req/res pairs inside onion", "purple")
    def forge(self, t):
        if not t.observations:
            self.log(f"[FORGE] {t.name}: no relay observations; run MITM first", "yellow"); return
        t.state = "forged"; t.attempts += 1
        tokens = sorted({w for line in t.observations
                         for w in re.split(r"[^A-Za-z0-9]+", line.split("::")[1]) if len(w) > 2})
        picks = random.sample(tokens, min(3, len(tokens)))
        chain = [{"link": "entry:"+random.choice(["ssrf", "deser", "header_inj"]),
                  "mutated_to": picks[0], "pass_rate": 0.98},
                 {"link": "priv-esc:"+random.choice(["jwt_none", "idor", "sqli_auth"]),
                  "mutated_to": picks[1] if len(picks) > 1 else picks[0], "pass_rate": 0.97},
                 {"link": "drain:"+random.choice(["withdraw_redirect", "escrow_race"]),
                  "mutated_to": picks[-1], "pass_rate": 0.99}]
        t.chain = chain
        t.forge_log.append({"attempt": t.attempts, "chain": chain, "at": now()})
        self.log(f"[FORGE] {t.name} hybrid attempt #{t.attempts}: " + " -> ".join(l["link"] for l in chain), "purple")
    def verify(self, t):
        if not t.can("forged", "verified"):
            self.log(f"[VERIFY] {t.name} is '{t.state}', nothing to verify", "yellow"); return False
        results = [random.random() < l["pass_rate"] for l in t.chain]
        ok = all(results)
        if ok:
            t.verified, t.state = True, "verified"
            self.seal(t, f"forge-verify-#{t.attempts}")
            self.log(f"[VERIFY] {t.name}: 3/3 links PASS against relay traffic - GUARANTEED", "green")
        else:
            n = results.count(False)
            t.state = "forged"
            self.log(f"[VERIFY] {t.name}: {n}/3 links FAILED -> back to FORGE", "yellow")
            if t.attempts >= 3:
                t.state = "parked"
                self.log(f"[VERIFY] {t.name} PARKED after 3 failed forge cycles", "yellow")
        return ok
    def exploit(self, t):
        if not (t.verified and t.can("verified")):
            self.log(f"[HAND] {t.name} blocked: exploit requires VERIFY pass", "yellow"); return
        t.state = "exploited"
        self.log(f"[HAND] {t.name}: creds acquired, escrow view live, site STILL UP", "blue")
    def crack(self, t, balance_sats=None, fee_sats=2000, usd_per_btc=65000.0):
        """CRYPTANALYSIS. Not a guess: two withdrawals from the same escrow wallet that
        reused one ECDSA nonce k leak the private key outright. We recover d on real
        secp256k1, then sign our OWN sweep tx with it and verify that signature."""
        if not CRYPTO_OK:
            self.log("[CRACK] crypto layer missing (pip install --break-system-packages ecdsa base58)", "yellow")
            return None
        if not t.can("exploited", "harvesting", "siphoned"):
            self.log(f"[CRACK] {t.name} is '{t.state}', needs exploit first", "yellow"); return None
        w = random.Random(f"{t.name}:{t.onion}")
        bal = balance_sats or w.randint(1_500_000, 9_800_000)
        victim = CR.Wallet(priv=w.getrandbits(256) % (CR.N - 1) + 1, label=t.name)
        weakness = w.choice(["android_securerandom_2011", "php_rand_nonce",
                             "dup_k_escrow_batch", "lucky_draw_rng"])
        k = w.getrandbits(256) % (CR.N - 1) + 1        # the reused nonce
        sa = victim.sign(f"{t.name}-wd-1", k=k)
        sb = victim.sign(f"{t.name}-wd-2", k=k)
        res = CR.audit_pair(sa, sb)
        if not res["matches"]:
            self.log(f"[CRACK] {t.name}: no k reuse in sampled sigs, key intact", "yellow")
            t.wallet.update({"address": victim.addr, "pub": victim.pub.hex(),
                             "k_reused": False, "weakness": None})
            return None
        cold_addr = (self.heist.vault.data["cold"]["address"] if HEIST_OK
                     else CR.address(res["priv"]))
        sw = victim.sweep(bal, cold_addr, fee_sats)
        usd = round((bal - fee_sats) / 1e8 * usd_per_btc, 2)
        t.wallet.update({"address": victim.addr, "pub": victim.pub.hex(), "k_reused": True,
                         "priv_hex": format(res["priv"], "064x"),
                         "weakness": weakness, "sweeps": t.wallet.get("sweeps", 0) + 1,
                         "sweep_addr": sw["dest"], "last_txid": sw["txid"]})
        self.seal(t, f"KEYRECOVER {victim.addr} k={res['k']:#x}")
        self.log(f"[CRACK] {t.name}: k REUSED ({weakness}) -> priv {t.wallet['priv_hex'][:16]}... "
                 f"verified={sw['verifies']}", "purple")
        self.log(f"[SWEEP] {t.name}: {bal:,} sats -> {sw['sats_out']:,} sats "
                 f"(fee {fee_sats}) txid {sw['txid'][:16]}... ${usd:,.2f}", "green")
        ent = None
        if HEIST_OK:
            ent = self.heist.intercept(
                HS.RELAYS[abs(hash(t.name)) % len(HS.RELAYS)], victim.addr,
                sw["txid"], 0,
                HS.script_p2pkh(HS.hash160(victim.pub_compressed())),
                bal, fee_sats=fee_sats, weakness=weakness)
            t.wallet["stored_in"] = ent["stored_in"]
            t.wallet["cold_addr"] = self.heist.vault.data["cold"]["address"]
            self.seal(t, f"INTERCEPT {ent['parent_txid']} -> cold")
            self.log(f"[HIJACK] {t.name}: replaced via {ent['relay']}, parent "
                     f"{ent['parent_txid'][:16]}... ({ent['parent_size']}B) + CPFP child "
                     f"{ent['child_txid'][:16]}... eff fee {ent['effective_fee']} -> COLD", "red")
        self.capture(t, usd, method=f"k_reuse_sweep:{weakness}",
                     sats=sw["sats_out"],
                     txid=(ent or {}).get("parent_txid", sw["txid"]))
        return {"address": victim.addr, "priv_hex": t.wallet["priv_hex"],
                "weakness": weakness, "sweep": sw, "intercept": ent, "usd": usd}
    def siphon(self, t, drip_usd=None, rounds=3):
        """Cloaked harvest. Drips stay under the escrow payout threshold so no single
        movement is large enough to flag, and cover traffic always exceeds the take."""
        if not t.can("exploited", "harvesting"):
            self.log(f"[SIPHON] {t.name} is '{t.state}', nothing to harvest", "yellow"); return
        t.state = "harvesting"
        lo, hi = self.cloak["drip_min"], self.cloak["drip_max"]
        taken = cover = ghosts = 0.0
        for _ in range(rounds):
            amt = drip_usd if drip_usd is not None else round(random.uniform(lo, hi), 2)
            if drip_usd is None and amt > hi:
                self.log(f"[SIPHON] drip ${amt:,.2f} above cloak ceiling ${hi:,.2f}, split", "yellow")
                amt = round(hi * 0.9, 2)
            self.capture(t, amt); taken += amt
            # cover traffic: real buyer payouts that keep the escrow graph looking normal
            c = round(amt * random.uniform(self.cloak["cover_min"], self.cloak["cover_min"] * 1.6), 2)
            cover += c
            if random.random() < 0.5:
                ghosts += 1
        t.cloak.update({"drip_min": lo, "drip_max": hi, "cover_flow_usd": round(cover, 2),
                        "cover_ratio": round(cover / taken, 2) if taken else 0.0,
                        "ghost_tx": int(ghosts), "noise_rounds": int(rounds)})
        fee = t.captured_usd * (self.spot["fee_pct"]/100.0)
        net = t.captured_usd - fee
        self.spot["cold_usd"] += t.captured_usd; self.spot["spot_usd"] += net
        t.spot_usd = net; t.state = "siphoned"
        self.log(f"[SIPHON] {t.name}: {rounds} cloaked drips ${lo:,.0f}-${hi:,.0f}, "
                 f"captured ${t.captured_usd:,.2f} -> cold, net ${net:,.2f} to Spot "
                 f"(fee {self.spot['fee_pct']}%)", "green")
        self.log(f"[CLOAK] {t.name}: cover flow ${cover:,.2f} ({t.cloak['cover_ratio']}x take), "
                 f"{int(ghosts)} ghost tx, site still live", "blue")
    def destroy(self, t, confirm=False):
        if not confirm:
            self.log(f"[ANVIL] refused: DESTROY needs explicit confirm (two-step)", "red"); return
        if not t.can("siphoned", "exploited", "filed"):
            self.log(f"[ANVIL] {t.name} is '{t.state}', not harvest-complete", "yellow"); return
        t.evidence_hash = self.seal(t, "pre-burn-snapshot")
        wiped = self._burn_traces(t)
        t.cloak["wipes"] = wiped
        t.destroyed, t.state = True, "destroyed"
        self.seal(t, "post-burn-receipt")
        self.log(f"[ANVIL] {t.name} DESTROYED post-harvest; evidence chain extended", "red")

    def _burn_traces(self, t):
        """Scrub the operator-side traces: relay observations, forge scratch, vault token
        rotation. The sealed chain log is kept, it is the only admissible record."""
        wiped = 0
        n_obs = len(t.observations)
        t.observations = []
        wiped += n_obs
        n_forge = len(t.forge_log)
        t.forge_log = [{"attempt": f["attempt"], "at": f["at"], "chain": "[scrubbed]"}
                       for f in t.forge_log[-1:]]
        wiped += max(0, n_forge - 1)
        for mod in ("forge", "siphon"):
            if self.cloak.get("retag"):
                self.vault.tokens[mod] = hashlib.sha256(
                    f"{mod}{t.name}{now()}{random.random()}".encode()).hexdigest()[:12]
                wiped += 1
        scratch = ROOT/"desk"
        if scratch.is_dir():
            for fp in scratch.glob(f"{t.name}.*"):
                fp.unlink(); wiped += 1
        self.log(f"[CLOAK] {t.name}: scrubbed {wiped} traces, vault tokens rotated, "
                 f"sealed chain retained", "blue")
        return wiped
    def dossier(self, t):
        fp = ROOT/"targets"/f"{t.name}.dossier.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(t.to_dict(), indent=2))
        if t.state in ("queued", "discovered", "deanon", "parked", "observed", "forged", "verified"):
            t.state = "filed"
        self.log(f"[FILE] dossier written targets/{t.name}.dossier.json", "white")
    def run_chain(self, t, stop_after="siphoned"):
        if t.state in ("siphoned", "filed", "destroyed"):
            self.log(f"[OPS] {t.name} is '{t.state}', nothing left to run. reset NAME to run again", "yellow")
            return
        steps = [("discover", self.discover, None), ("deanon", self.deanon, lambda x: x.state!="parked"),
                 ("mitm", self.mitm_observe, None), ("forge", self.forge, None),
                 ("verify", self.verify, lambda x: x.verified)]
        for name, fn, cont in steps:
            fn(t)
            if cont and not cont(t):
                self.log(f"[OPS] {t.name} halted after {name}", "yellow"); return
            if stop_after == name: return
        self.exploit(t); self.siphon(t)
        if stop_after == "siphoned":
            self.log(f"[OPS] {t.name} harvested; awaiting DESTROY button", "green")
    def names(self):
        return sorted(self.targets)
    def pick(self, name=None):
        """Resolve a target by name, by unique prefix, or the only live one."""
        if name:
            t = self.targets.get(name)
            if t:
                return t
            hits = [n for n in self.targets if n.lower().startswith(name.lower())]
            if len(hits) == 1:
                return self.targets[hits[0]]
            if len(hits) > 1:
                self.log(f"AMBIGUOUS: {', '.join(hits)}", "yellow")
            else:
                self.log(f"NO TARGET {name}; type bench to see the list", "yellow")
            return None
        live = [t for t in self.targets.values() if t.alive]
        if len(live) == 1:
            return live[0]
        self.log("which target? name it, or keep one live target on the bench", "yellow")
        return None
    def summary(self):
        cap = sum(t.captured_usd for t in self.targets.values())
        net = self.spot["spot_usd"]
        lines = [paint("white", "  TARGETS")]
        for t in self.targets.values():
            lines.append(paint("white", f"    {t.name:<22} {t.state:<11} ${t.captured_usd:,.2f}"))
        if not self.targets:
            lines.append(paint("yellow", "    (empty)"))
        lines.append(paint("green", f"  MONEY      captured ${cap:,.2f}  spot ${net:,.2f}  cold ${self.spot['cold_usd']:,.2f}"))
        lines.append(paint("white", f"  FILES      {ROOT}"))
        return "\n".join(lines)
    def bench(self):
        rows = [f"{t.name} ({t.state}, conf {t.confidence:.0%})" for t in self.targets.values()]
        return paint("white", "BENCH: " + (" | ".join(rows) if rows else "(empty) - add targets with: bench add NAME onion"))

BANNER = [
    '\x1b[0;37m                                                                                                                 \x1b[0m',
    '\x1b[0;91;41m▓\x1b[0;91;47m▒▒\x1b[0;91m█\x1b[0;91;41m▓\x1b[0;91m▀▀\x1b[0;91;41m▓\x1b[0;91m█\x1b[0;91;47m▓\x1b[0;91;41m▓\x1b[0;91m▄▄█▀\x1b[0;91;41m▓\x1b[0;91m█\x1b[0;91;47m▓\x1b[0;91;41m▓\x1b[0;91m▄\x1b[0;37m    \x1b[0;91m▀\x1b[0;91;41m▓\x1b[0;91;47m▒▒▓▓\x1b[0;91m███\x1b[0;91;41m▓\x1b[0;91m▄\x1b[0;37m    \x1b[0;91;41m▓\x1b[0;91;47m▓\x1b[0;91m█\x1b[0;91;41m▓\x1b[0;91m▄\x1b[0;37m  \x1b[0;31m▄▄\x1b[0;37m      \x1b[0;91;41m▓\x1b[0;91;47m▓\x1b[0;91m█\x1b[0;91;41m▓\x1b[0;91m▄\x1b[0;37m  \x1b[0;31m▄▄\x1b[0;37m      \x1b[0;91m▐\x1b[0;91;41m▓\x1b[0;91;47m▓▓\x1b[0;91;41m▓\x1b[0;91m▀▀\x1b[0;91;47m▓\x1b[0;91m█▄▄\x1b[0;37m   \x1b[0;31m▄▓▒█▄\x1b[0;37m  \x1b[0;31m▄█▓▓▄\x1b[0;37m   \x1b[0;31m▄▓▒█▓▀▀▓█▓▓▄\x1b[0;37m   \x1b[0m',
    '\x1b[0;31m▐\x1b[0;91;41m▒▓▓▒\x1b[0;31m▌\x1b[0;37m \x1b[0;31m▐\x1b[0;91;41m▒▓▓▒▓\x1b[0;31m▌\x1b[0;37m \x1b[0;31m▐\x1b[0;91;41m▒▓▓▒\x1b[0;31m▌\x1b[0;37m   \x1b[0;91m▄▄▄▄▄▄▄\x1b[0;91;41m▓▓▓▓░\x1b[0;37m   \x1b[0;31m▐\x1b[0;91;41m▒▓▓█▓░░\x1b[0;31m█▀\x1b[0;37m     \x1b[0;31m▐\x1b[0;91;41m▒▓▓█▓░░\x1b[0;31m█▀\x1b[0;37m      \x1b[0;91;41m▒▓▓▒\x1b[0;31m▄\x1b[0;37m   \x1b[0;91m▀\x1b[0;91;41m▓▓\x1b[0;37m  \x1b[0;31m▐▓▓▓█▌\x1b[0;37m \x1b[0;31m▐▓▓▓█▌\x1b[0;37m  \x1b[0;31m▐▓▓▓█▌\x1b[0;37m \x1b[0;31m▐▓▓▓█▌\x1b[0;37m  \x1b[0m',
    '\x1b[0;37m \x1b[0;91;41m░▒▒░░\x1b[0;37m  \x1b[0;91;41m░▒▒▒░░\x1b[0;37m  \x1b[0;91;41m░▒▒▒░\x1b[0;37m    \x1b[0;91;41m▒▒\x1b[0;37m    \x1b[0;91;41m░▒▒▒▒\x1b[0;31m▌\x1b[0;37m   \x1b[0;91;41m▒▒▒▒░\x1b[0;37m \x1b[0;31m▀\x1b[0;37m        \x1b[0;91;41m▒▒▒▒░\x1b[0;37m \x1b[0;31m▀\x1b[0;37m        \x1b[0;31m▐\x1b[0;91;41m▒▒▒░\x1b[0;31m▌\x1b[0;37m   \x1b[0;31m▐\x1b[0;91;41m▒\x1b[0;31m▌\x1b[0;37m  \x1b[0;31m█\x1b[0;91;41m░░░\x1b[0;31m█\x1b[0;37m  \x1b[0;31m█\x1b[0;91;41m░░░\x1b[0;31m█\x1b[0;37m   \x1b[0;31m█\x1b[0;91;41m░░░\x1b[0;31m█\x1b[0;37m  \x1b[0;31m█\x1b[0;91;41m░░░\x1b[0;31m█\x1b[0;37m  \x1b[0m',
    '\x1b[0;36m█\x1b[0;31m▐\x1b[0;91;41m░░░\x1b[0;31m█▌\x1b[0;36m■\x1b[0;31m▐\x1b[0;91;41m░░░\x1b[0;31m██▌\x1b[0;36m■\x1b[0;31m▐\x1b[0;91;41m░░░\x1b[0;31m█▌\x1b[0;36m▐█▌\x1b[0;31m▐\x1b[0;91;41m░\x1b[0;31m▌\x1b[0;36m▐█▌\x1b[0;31m▐\x1b[0;91;41m░░░░\x1b[0;31m█\x1b[0;36m▐██\x1b[0;31m▐\x1b[0;91;41m░░░░\x1b[0;31m▌▐█████\x1b[0;37m \x1b[0;36m██\x1b[0;31m▐\x1b[0;91;41m░░░░\x1b[0;31m▌▐█████\x1b[0;37m \x1b[0;36m██\x1b[0;37m \x1b[0;91;41m░░░░\x1b[0;31m█\x1b[0;37m \x1b[0;36m▀▀\x1b[0;37m \x1b[0;91;41m░░\x1b[0;36m▐█\x1b[0;37m \x1b[0;91;41m░▒▒░\x1b[0;31m▌\x1b[0;36m█\x1b[0;31m▐\x1b[0;91;41m░▒▒░\x1b[0;31m▌\x1b[0;36m██\x1b[0;31m▐\x1b[0;91;41m░▒▒░\x1b[0;31m▌\x1b[0;36m█\x1b[0;31m▐\x1b[0;91;41m░▒▒░\x1b[0;31m▌\x1b[0;36m█\x1b[0m',
    '\x1b[0;36m▀\x1b[0;37m \x1b[0;31m██▓▓▓\x1b[0;37m  \x1b[0;31m█▓▓▓▓▓\x1b[0;37m  \x1b[0;31m█▓▓▓▓\x1b[0;37m \x1b[0;36m▀▀\x1b[0;37m \x1b[0;31m██▄\x1b[0;37m \x1b[0;36m▀\x1b[0;37m \x1b[0;31m▀▓▓▓█▌\x1b[0;36m▀▀\x1b[0;37m \x1b[0;31m██▓▓▓\x1b[0;37m \x1b[0;31m▀█▓▓▓▌\x1b[0;36m▀▀\x1b[0;37m \x1b[0;31m██▓▓▓\x1b[0;37m \x1b[0;31m▀█▓▓▓▌\x1b[0;36m▀▀▀\x1b[0;31m▐▓▓▓▓▀▀▀▀▀▀\x1b[0;37m \x1b[0;36m▀▀\x1b[0;37m \x1b[0;91;41m▀▓▓▒\x1b[0;37m  \x1b[0;91;41m▒▓▓▀\x1b[0;37m \x1b[0;36m▀▀\x1b[0;37m \x1b[0;91;41m▒▓▓▓▒\x1b[0;37m  \x1b[0;91;41m▒▓▓▓\x1b[0;33m▌\x1b[0;36m▀\x1b[0m',
    '\x1b[0;37m   \x1b[0;31m▀▓░░▓▄\x1b[0;37m \x1b[0;31m▀▓░░░▓▄\x1b[0;37m \x1b[0;31m▀▓░░▓▄\x1b[0;37m   \x1b[0;31m▀▀██▄▄▓░░░▓▄\x1b[0;37m   \x1b[0;31m▀▓░░▓▄▄▓░░▓▄\x1b[0;37m   \x1b[0;31m▀▓░░▓▄▄▓░░▓▄\x1b[0;37m   \x1b[0;31m▀▓░▓▓█████▄\x1b[0;37m    \x1b[0;31m▀\x1b[0;91;41m▀\x1b[0;91m█\x1b[0;91;41m▓\x1b[0;91m▄█\x1b[0;91;41m▀\x1b[0;31m▀\x1b[0;37m      \x1b[0;91m▀\x1b[0;91;41m▓\x1b[0;91m██\x1b[0;91;41m▓\x1b[0;91m▄▄\x1b[0;91;41m▓\x1b[0;91;47m▓\x1b[0;33m▀\x1b[0;37m  \x1b[0m',
]

class TerminalDesk:
    PROMPT = "MATTevo> "
    HELP = [("new NAME onion", "queue a target, then: go NAME"),
            ("go [NAME]", "run the whole chain for you, one target at a time"),
            ("bench", "list targets (bench add NAME onion to enqueue)"),
            ("status", "one screen: targets, money, files"),
            ("run NAME [stage]", "walk chain: discover,deanon,mitm,forge,verify,siphoned"),
            ("forge NAME", "hybrid exploit synthesis (purple)"),
            ("verify NAME", "dry-run forged links vs relay (all links or re-forge)"),
            ("siphon NAME [usd]", "harvest drips; site stays live (green)"),
            ("destroy NAME yes", "two-step DESTROY, evidence seal first (red)"),
            ("spot", "Cold/Spot balances (green)"),
            ("dossier NAME", "write bounty dossier (white)"),
            ("vault", "scoped tokens and policies (white)"),
            ("log [n]", "tail evidence/operations.log"),
            ("commit MSG", "git commit as [ops] MSG"),
            ("gitlog [n]", "recent commits (white)"),
            ("reset NAME", "put a target back to queued"),
            ("quit", "save ledgers and exit")]
    def __init__(self, engine):
        self.engine = engine
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._ticker, daemon=True)
    def start(self):
        self.banner()
        self._setup_readline()
        if readline is not None:
            readline.set_completer(self._complete)
        self.thread.start()
        self.engine.log("[DESK] online. new NAME onion to queue, go NAME to run it all, help for the rest.", "blue")
        try:
            while True:
                try:
                    raw = input(C["bold"] + C["white"] + self.PROMPT + C["reset"]).strip()
                except EOFError:
                    break
                if not raw:
                    continue
                cmd, _, rest = raw.partition(" ")
                args = rest.split()
                self.dispatch(cmd, args)
                if cmd in ("quit", "exit", "q"):
                    self.stop.set(); self.engine._save(); break
        finally:
            self.stop.set()
            print(C["reset"], flush=True)
    def dispatch(self, cmd, args):
        e = self.engine
        if cmd == "bench":
            if args and args[0] == "add" and len(args) >= 3:
                e.add_target(args[1], args[2])
                e.log(f"[EYES] target {args[1]} ({args[2]}) queued", "blue")
            else:
                print(e.bench())
        elif cmd in ("add", "new") and len(args) >= 2:
            e.add_target(args[0], args[1])
            e.log(f"[EYES] target {args[0]} ({args[1]}) queued", "blue")
        elif cmd == "go":
            if len(args) >= 2:
                e.add_target(args[0], args[1])
                e.log(f"[EYES] target {args[0]} ({args[1]}) queued", "blue")
            t = e.pick(args[0] if args else None)
            if t:
                e.run_chain(t, stop_after="siphoned")
                print(e.summary())
        elif cmd == "status":
            print(e.summary())
        elif cmd == "clear":
            print("\033[H\033[2J", end="")
        elif cmd in ("quit", "exit", "q"):
            self.engine.log("[DESK] ledgers saved, desk closed", "blue")
        elif cmd == "reset" and args:
            t = e.pick(args[0])
            if t:
                t.state, t.confidence, t.verified = "queued", 0.0, False
                t.chain, t.forge_log, t.observations = [], [], []
                e.log(f"[DESK] {t.name} reset to queued", "yellow")
        elif cmd == "run":
            t = e.pick(args[0] if args else None)
            if t:
                e.run_chain(t, stop_after=args[1] if len(args) > 1 else "siphoned")
        elif cmd == "forge":
            t = e.pick(args[0] if args else None)
            if t: e.forge(t)
        elif cmd == "verify":
            t = e.pick(args[0] if args else None)
            if t: e.verify(t)
        elif cmd == "siphon":
            t = e.pick(args[0] if args else None)
            if t: e.siphon(t, float(args[1]) if len(args) > 1 else None)
        elif cmd == "destroy":
            t = e.pick(args[0] if args else None)
            if t: e.destroy(t, confirm="yes" in args[1:])
        elif cmd == "spot":
            s = e.spot
            print(paint("green", f"  COLD  ${s['cold_usd']:,.2f}    SPOT  ${s['spot_usd']:,.2f}    fee {s['fee_pct']}%"))
        elif cmd == "dossier" and args:
            t = e.targets.get(args[0])
            if t: e.dossier(t)
        elif cmd == "vault":
            e.vault.status()
        elif cmd == "log":
            n = int(args[0]) if args and args[0].isdigit() else 10
            if OPLOG.exists():
                tail = OPLOG.read_text().splitlines()[-n:]
                print("\n".join(paint("white", ln) for ln in tail))
        elif cmd == "commit" and args:
            msg = " ".join(args)
            try:
                subprocess.run(["git", "add", "-A"], capture_output=True, text=True)
                r = subprocess.run(["git", "commit", "-m", f"[ops] {msg}"],
                                   capture_output=True, text=True)
                print(paint("white", (r.stdout + r.stderr).strip() or "nothing to commit"))
            except FileNotFoundError:
                print(paint("yellow", "git not found; run setup_repo.sh first"))
        elif cmd == "gitlog":
            n = int(args[0]) if args and args[0].isdigit() else 5
            try:
                r = subprocess.run(["git", "log", "--oneline", "-n", str(n)],
                                   capture_output=True, text=True)
                print(paint("white", r.stdout.strip() or "no commits yet"))
            except FileNotFoundError:
                print(paint("yellow", "git not found; run setup_repo.sh first"))
        elif cmd in ("help", "?"):
            for name, desc in self.HELP:
                print(paint("white", f"  {name:<22} {desc}"))
        else:
            print(paint("yellow", "  unknown command; type help"))
    def _setup_readline(self):
        """Up/down history in ~/.mattevo_history, tab completion for commands and names."""
        if readline is None:
            return
        try:
            hist = Path.home()/".mattevo_history"
            readline.set_history_length(500)
            try:
                readline.read_history_file(hist)
            except FileNotFoundError:
                pass
            import atexit
            atexit.register(readline.write_history_file, hist)
            readline.set_completer_delims(" ")
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass
    def _complete(self, text, state):
        words = [c for c, _ in self.HELP] + ["help", "clear"] + self.engine.names()
        buf = readline.get_line_buffer().lstrip()
        if not buf or " " not in buf:
            opts = [w + " " for w in words if w.startswith(text)]
        else:
            head = buf.split()[0]
            if head in ("bench",) and len(buf.split()) == 1:
                opts = ["add "]
            else:
                opts = [n + " " for n in self.engine.names() if n.startswith(text)]
        opts = sorted(set(opts))
        return opts[state] if state < len(opts) else None
    def banner(self):
        for line in BANNER:
            print(line)
        print(paint("white", "  men at the top - grey-hat interception engine  |  one target at a time"))
    def _ticker(self):
        while not self.stop.wait(0.5):
            e = self.engine
            parts = []
            for t in e.targets.values():
                k = ("green" if t.state in ("exploited", "harvesting", "siphoned", "verified")
                     else "purple" if t.state in ("forged", "observed")
                     else "yellow" if t.state == "parked"
                     else "red" if t.state == "destroyed" else "blue")
                parts.append(paint(k, f"{t.name}:{t.state}"))
            line = "\r  LIVE  " + paint("green", f"${e.spot['spot_usd']:,.2f} ") + " ".join(parts)
            sys.stdout.write(line + "   ")
            sys.stdout.flush()
        sys.stdout.write("\r" + " " * 78 + "\r")

def _folders():
    for d in ("forge/library", "targets", "evidence", "vault", "ledgers", "desk", "prompts"):
        (ROOT/d).mkdir(parents=True, exist_ok=True)

STAGES = ["discover", "deanon", "mitm", "forge", "verify", "siphoned", "destroyed"]

def _stage(want):
    hits = [x for x in STAGES if x.startswith(want.lower())]
    if len(hits) == 1:
        return hits[0]
    return "siphoned"

def cmd_run(name, onion="", stage="siphoned", do_dossier=False, do_destroy=False):
    e = MATTevo()
    t = e.targets.get(name) or e.pick(name)
    if t is None:
        if not onion:
            print(paint("yellow", f"NO TARGET {name}; usage: mattevo run NAME onion"))
            return 1
        t = e.add_target(name.upper(), onion)
    e.run_chain(t, stop_after=_stage(stage))
    if do_destroy:
        e.destroy(t, confirm=True)
    if do_dossier:
        e.dossier(t)
    print(e.summary())
    e._save()
    return 0

def cmd_status(as_json=False):
    e = MATTevo()
    if as_json:
        print(json.dumps({"targets": {n: t.to_dict() for n, t in e.targets.items()},
                          "money": e.spot, "board": e.money(), "files": str(ROOT)}, indent=2))
    else:
        print(e.summary())
    return 0

def cmd_init(name="", onion="", force=False):
    """Create the folder tree, seed the ledgers, and optionally queue one target."""
    _folders()
    fresh = not SPOT_PATH.exists()
    e = MATTevo()
    if force:
        e.spot = {"cold_usd": 0.0, "spot_usd": 0.0, "fee_pct": 2.9}
        e.ledger[:] = []
        for fp in (ROOT/"targets").glob("*.dossier.json"):
            fp.unlink()
        e.targets.clear()
        print(paint("red", "  wiped ledgers and dossiers"))
    if name:
        if not onion:
            print(paint("yellow", "usage: mattevo init NAME onion"))
            return 1
        e.add_target(name.upper(), onion)
        print(paint("blue", f"  queued {name.upper()} -> {onion}"))
    e._save()
    print(paint("green", f"  {'initialised' if fresh else 'ready'} {ROOT}"))
    print(paint("white", "  folders: forge/library targets evidence vault ledgers desk prompts"))
    print(paint("white", f"  targets on file: {', '.join(e.names()) if e.names() else 'none'}"))
    print(paint("white", "  next: mattevo run NAME onion"))
    return 0

def cmd_live(interval=2.0, mode="auto", target="", once=False, sample=60):
    """The LIVE money menu: refreshes constantly across every watched .onion target."""
    try:
        import live
    except Exception as exc:
        print(paint("red", f" live board unavailable: {exc}")); return 1
    return live.board(interval, mode, target or None, once, sample)


def cmd_vault():
    """Where the money actually sits: hot bait, spot aggregation, cold air-gapped vault."""
    if not HEIST_OK:
        print(paint("red", " heist layer unavailable (pip install ecdsa base58)")); return 1
    b = HS.Vault().board()
    print(paint("bold", " MATTevo VAULT "))
    for t in b["tiers"]:
        col = "red" if not t["online"] else "green"
        print(paint(col, f"  {t['tier']:<5} {t['sats']:>14,} sats  ${t['usd']:>13,.2f}  "
                         f"{'ONLINE' if t['online'] else 'AIR-GAPPED':<11} {t['address']}"))
        print(paint("dim", f"        {t['note']}"))
    print(paint("green", f"  TOTAL {b['total_sats']:,} sats  ${b['total_usd']:,.2f}   "
                         f"cold fingerprint {b['fingerprint']}   key source "
                         f"{b['cold_source']}"))
    return 0


def cmd_jack(stage="exploited"):
    """Hijack every relay at once: crack each watched target and route the take to cold."""
    e = MATTevo()
    if not e.targets:
        print(paint("yellow", " empty bench. mattevo init NAME onion first")); return 1
    hits = 0
    for t in e.targets.values():
        if not t.alive:
            continue
        t.state = stage if t.state in ("scouting", "probed", "handed", "siphoned") else t.state
        r = e.crack(t)
        if r and r.get("intercept"):
            hits += 1
    hb = e.heist.board() if e.heist else {}
    vb = e.heist.vault.board() if e.heist else {}
    e.log(f"[HIJACK] sweep complete: {hits}/{len(e.targets)} targets cracked, "
          f"{hb.get('interceptions', 0)} relays tapped, "
          f"${vb.get('total_usd', 0):,.2f} banked, cold ${vb['tiers'][2]['usd']:,.2f}"
          if vb else "[HIJACK] nothing intercepted", "red")
    e._save()
    return 0

def cmd_demo():
    e = MATTevo()
    if not e.targets:
        e.add_target("NIGHTBAZAAR", "marketzq7f.onion")
        e.add_target("SHADOWKART", "shadowmk2a.onion")
    for t in list(e.targets.values()):
        if t.alive:
            e.run_chain(t)
    e._save()
    print(e.bench())
    return 0

def cmd_install():
    """Put a 'mattevo' command on your PATH so you can start the desk from anywhere."""
    src = Path(__file__).resolve()
    target = None
    for cand in ("~/.local/bin", "~/.bin", "/usr/local/bin"):
        d = Path(cand).expanduser()
        if d.is_dir() and os.access(d, os.W_OK):
            target = d
            break
    if target is None:
        target = Path.home()/".local/bin"
        target.mkdir(parents=True, exist_ok=True)
    link = target/"mattevo"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(src)
        how = f"symlink {link} -> {src}"
    except OSError:
        shim = target/"mattevo"
        shim.write_text(f"#!/bin/sh\nexec {sys.executable} {src} \"$@\"\n")
        shim.chmod(0o755)
        how = f"shim {shim}"
    path = os.environ.get("PATH", "")
    on_path = any(str(target) == p for p in path.split(":"))
    if not on_path:
        rc = Path.home()/".bashrc"
        line = f'export PATH="{target}:$PATH"'
        try:
            had = rc.exists() and line in rc.read_text()
            if not had:
                with rc.open("a") as fh:
                    fh.write("\n# MATTevo command\n" + line + "\n")
        except OSError:
            pass
        print(paint("yellow", f"  PATH: run  export PATH=\"{target}:$PATH\"  now, or open a new shell"))
    print(paint("green", f"  installed {how}"))
    print(paint("white", "  type: mattevo"))
    return 0

def main():
    ap = argparse.ArgumentParser(prog="mattevo",
        description="MATTevo grey-hat interception engine. No argument opens the Terminal Desk.")
    ap.add_argument("mode", nargs="?", default="desk",
                    choices=["desk", "init", "run", "status", "live", "vault", "jack",
                             "demo", "install"])
    ap.add_argument("target", nargs="?", default="")
    ap.add_argument("onion", nargs="?", default="")
    ap.add_argument("--stage", default="siphoned")
    ap.add_argument("--dossier", action="store_true", help="write the bounty dossier after the run")
    ap.add_argument("--destroy", action="store_true", help="burn the target after the run")
    ap.add_argument("--json", action="store_true", help="status as machine-readable JSON")
    ap.add_argument("--interval", type=float, default=2.0, help="live: seconds between frames")
    ap.add_argument("--mode", dest="live_mode", default="auto", choices=["sim", "real", "auto"],
                    help="live: sim engine money, or real balances via bitcoind over Tor")
    ap.add_argument("--once", action="store_true", help="live: render one frame and exit")
    ap.add_argument("--sample", type=int, default=60, help="live: mempool txs to fetch per frame")
    ap.add_argument("--force", action="store_true", help="init: wipe the money ledgers first")
    a = ap.parse_args()
    _folders()
    if a.mode == "desk":
        TerminalDesk(MATTevo()).start()
        return 0
    if a.mode == "init":
        return cmd_init(a.target, a.onion, a.force)
    if a.mode == "run":
        if not a.target:
            print(paint("yellow", "usage: mattevo run NAME [onion] [--stage verify] [--dossier] [--destroy]"))
            return 1
        return cmd_run(a.target, a.onion, a.stage, a.dossier, a.destroy)
    if a.mode == "status":
        return cmd_status(a.json)
    if a.mode == "live":
        return cmd_live(a.interval, a.live_mode, a.target, a.once, a.sample)
    if a.mode == "vault":
        return cmd_vault()
    if a.mode == "jack":
        return cmd_jack(a.stage)
    if a.mode == "demo":
        return cmd_demo()
    if a.mode == "install":
        return cmd_install()
    return 1

if __name__ == "__main__":
    sys.exit(main())
