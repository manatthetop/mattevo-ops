# MATTevo

Grey-hat dark-web interception engine. One target at a time. Money ledger and evidence locker stay split.
VERIFY gates every forged exploit against observed relay traffic before it may fire.

## Install the shortcut

```bash
cd ~/MATTevo
python3 mattevo.py install
exec bash
```

That puts `mattevo` on your PATH. Any terminal, any folder, from then on:

```bash
mattevo        # open the Terminal Desk
```

Re-run `mattevo install` if you move the folder.

## Work without the Desk

Three commands cover a whole operation.

```bash
mattevo init NIGHTBAZAAR marketzq7f.onion   # build folders, seed ledgers, queue the target
mattevo run night --dossier --destroy       # run the chain, file the dossier, burn it
mattevo status                              # targets, money, file paths
```

| Command | What it does |
|---|---|
| `mattevo` | open the Terminal Desk |
| `mattevo init NAME onion` | create the folder tree, seed the ledgers, queue one target |
| `mattevo init --force` | wipe ledgers and dossiers, start the operation clean |
| `mattevo run NAME onion` | queue and run the full chain in one line |
| `mattevo run NAME` | run a target already on file |
| `mattevo status` | one screen: targets, money, files |
| `mattevo status --json` | the same for scripts and cron |
| `mattevo demo` | the two-target demo run |
| `mattevo install` | redo the `mattevo` shortcut |

Run flags:

| Flag | Effect |
|---|---|
| `--stage verify` | stop after any stage: discover, deanon, mitm, forge, verify, siphoned |
| `--dossier` | write `targets/NAME.dossier.json` after the run |
| `--destroy` | burn the target after the run, evidence sealed first |

Names accept any unique prefix, so `night` finds `NIGHTBAZAAR`. A target that is parked below 50%
confidence halts on its own, that is the rule, not a fault. A harvested target refuses to re-run,
use `reset NAME` in the Desk or `mattevo init --force` to start over.

## The Desk, three commands

```
go NAME onion    queue a target and run the whole chain
status           one screen: targets, money, files
help             everything else
```

If only one target is live you can drop the name: `forge`, `verify`, `siphon`, `destroy yes`.
Up and down arrows replay history, Tab completes commands and target names.

## Colour code

- blue: system
- purple: forge
- white: data
- green: money
- red: burn and danger
- yellow: warning

## Folders

`forge/library` exploits, `targets` dossiers, `evidence` chain log and operations log,
`vault` scoped tokens, `ledgers` money and spot, `desk` and `prompts` scratch space.
All created on first run, all git-ignored so live operations never land in the repo.
