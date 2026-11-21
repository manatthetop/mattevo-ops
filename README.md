# MATTevo

Grey-hat dark-web interception engine. One target at a time. Money ledger and evidence locker are split.
VERIFY gates every forged exploit against observed relay traffic before it may fire.

## Install the shortcut

```bash
cd ~/MATTevo
python3 mattevo.py install
exec bash
```

That puts `mattevo` on your PATH. From then on, any terminal, any folder:

```bash
mattevo                 # open the Terminal Desk
mattevo status          # targets, money, file paths, then exit
mattevo run NAME onion  # one shot: full chain on one target
mattevo install         # redo the shortcut after moving the folder
```

## The Desk, three commands

```
go NAME onion    queue a target and run the whole chain
status           one screen: targets, money, files
help             everything else
```

Names can be shortened to any unique prefix, so `forge night` finds `NIGHTBAZAAR`.
If only one target is live you can drop the name: `verify`, `siphon`, `destroy yes`.
Up and down arrows replay your history, Tab completes commands and target names.

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
All created on first run.
