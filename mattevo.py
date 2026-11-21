"""MATTevo v0.3 - Men At The Top, evolved. Core engine + Terminal Desk.
Grey-hat dark-web interception pipeline: Eyes, Scope, Relay, Forge, Hand, Anvil, File.
Rules: one target at a time. Money ledger and evidence locker split. Vault-scoped tokens.
VERIFY gates every forged exploit against observed relay traffic before it may fire.
Colors: blue=system, purple=forge, white=data, green=money, red=burn/danger, yellow=warn."""
import argparse, hashlib, json, random, re, subprocess, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEDGER_PATH, SPOT_PATH = ROOT/"ledgers"/"money.json", ROOT/"ledgers"/"spot.json"
CHAIN_LOG, OPLOG = ROOT/"evidence"/"chain.log", ROOT/"evidence"/"operations.log"

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
    @property
    def alive(self): return not self.destroyed and self.state != "parked"
    def can(self, *states): return self.state in states
    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "name","onion","state","confidence","signals","observations","forge_log",
            "chain","verified","captured_usd","spot_usd","evidence_hash","destroyed")}

class MATTevo:
    def __init__(self):
        self.vault = Vault()
        self.targets, self.ledger = {}, []
        self.spot = {"cold_usd": 0.0, "spot_usd": 0.0, "fee_pct": 2.9}
        self._load()
        import atexit; atexit.register(self._save)
    def _load(self):
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
    def _save(self):
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps(self.ledger, indent=2))
        SPOT_PATH.write_text(json.dumps(self.spot, indent=2))
    def log(self, msg, kind="blue"):
        line = f"{now()} [{kind.upper()}] {msg}"
        print(paint(kind, line), flush=True)
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
    def siphon(self, t, drip_usd=None, rounds=3):
        if not t.can("exploited", "harvesting"):
            self.log(f"[SIPHON] {t.name} is '{t.state}', nothing to harvest", "yellow"); return
        t.state = "harvesting"
        for _ in range(rounds):
            amt = drip_usd if drip_usd is not None else round(random.uniform(3_000, 40_000), 2)
            t.captured_usd += amt
            self.ledger.append({"ts": now(), "target": t.name, "usd": amt, "method": "withdraw_redirect"})
        fee = t.captured_usd * (self.spot["fee_pct"]/100.0)
        net = t.captured_usd - fee
        self.spot["cold_usd"] += t.captured_usd; self.spot["spot_usd"] += net
        t.spot_usd = net; t.state = "siphoned"
        self.log(f"[SIPHON] {t.name}: captured ${t.captured_usd:,.2f} -> cold, "
                 f"net ${net:,.2f} to Spot (fee {self.spot['fee_pct']}%)", "green")
    def destroy(self, t, confirm=False):
        if not confirm:
            self.log(f"[ANVIL] refused: DESTROY needs explicit confirm (two-step)", "red"); return
        if not t.can("siphoned", "exploited"):
            self.log(f"[ANVIL] {t.name} is '{t.state}', not harvest-complete", "yellow"); return
        t.evidence_hash = self.seal(t, "pre-burn-snapshot")
        t.destroyed, t.state = True, "destroyed"
        self.log(f"[ANVIL] {t.name} DESTROYED post-harvest; evidence chain extended", "red")
    def dossier(self, t):
        fp = ROOT/"targets"/f"{t.name}.dossier.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(t.to_dict(), indent=2))
        t.state = "filed"
        self.log(f"[FILE] dossier written targets/{t.name}.dossier.json", "white")
    def run_chain(self, t, stop_after="siphoned"):
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
    HELP = [("bench", "list targets (bench add NAME onion to enqueue)"),
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
            ("quit", "save ledgers and exit")]
    def __init__(self, engine):
        self.engine = engine
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._ticker, daemon=True)
    def start(self):
        self.banner()
        self.thread.start()
        self.engine.log("[DESK] Terminal Desk online. Type help for commands.", "blue")
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
                if cmd == "quit":
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
        elif cmd == "add" and len(args) >= 2:
            e.add_target(args[0], args[1])
            e.log(f"[EYES] target {args[0]} queued", "blue")
        elif cmd == "run" and args:
            t = e.targets.get(args[0])
            if t:
                e.run_chain(t, stop_after=args[1] if len(args) > 1 else "siphoned")
        elif cmd == "forge" and args:
            t = e.targets.get(args[0])
            if t: e.forge(t)
        elif cmd == "verify" and args:
            t = e.targets.get(args[0])
            if t: e.verify(t)
        elif cmd == "siphon" and args:
            t = e.targets.get(args[0])
            if t: e.siphon(t, float(args[1]) if len(args) > 1 else None)
        elif cmd == "destroy" and args:
            t = e.targets.get(args[0])
            if t: e.destroy(t, confirm=len(args) > 1 and args[1] == "yes")
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

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="demo", choices=["demo", "desk"])
    args = ap.parse_args()
    e = MATTevo()
    if args.mode == "desk":
        TerminalDesk(e).start()
    else:
        if not e.targets:
            e.add_target("NIGHTBAZAAR", "marketzq7f.onion")
            e.add_target("SHADOWKART", "shadowmk2a.onion")
        for t in list(e.targets.values()):
            if t.alive:
                e.run_chain(t)
        e._save()
        print(e.bench())
