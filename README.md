# drivetest

> **Disclaimer.** Most of the code in this project was produced by Claude Code (Anthropic's agentic coding CLI), using the Claude Opus 4.8 (1M context) model, working iteratively with the author. Even though considerable effort went into keeping the quality reasonable, this tool performs **destructive disk writes**. It has not been independently audited; independent review is welcome and strongly advised before relying on it.

`drivetest` runs a health-and-integrity battery against a storage device: SMART baseline -> optional full write+verify (crc32c) -> read benchmarks -> SMART diff -> pass/fail. Logs go to a timestamped folder. It works for a drive in a USB enclosure (`/dev/sdX`) and for an NVMe drive in an M.2 slot (`/dev/nvmeXn1`).

The tool is a small dependency-free Python package (`src/drivetest/`, run via the `./drivetest` wrapper). It shells out to `fio`, `smartctl`, `nvme`, `lsblk`, `wipefs` and `findmnt`, parsing their JSON output. Stdlib-only on purpose, so it runs from a minimal live USB. See [Development](#development) for the layout and how to test it.

For a worked end-to-end example - screening a new SSD in a passive USB enclosure, then validating it in an M.2 slot - see [doc/case_ssd_swap.md](doc/case_ssd_swap.md).

## Usage

Everything runs as root (SMART and raw device IO need it), so the commands below use `sudo`.

Identify the device node and confirm it is the right one:

```bash
lsblk -o NAME,SIZE,MODEL,TRAN,SERIAL
```

Read-only health + read benchmarks (never writes):

```bash
sudo ./drivetest /dev/sdX
```

Destructive full write+verify (crc32c) then health + benchmarks (**wipes the drive**):

```bash
sudo ./drivetest --write /dev/sdX
```

Useful flags:

- `--quick` - write+verify only the first `quick_bytes` (`QUICK_BYTES` by default), for a fast sanity pass.
- `--parts N` - split the write+verify into N regions with a cooldown before each (see [Thermal pacing](#thermal-pacing-passive-enclosures)).
- `--only SPEC` - run just some of the N parts (e.g. `--only 1-4`), to break and resume.
- `--force` - override the blank-disk guard (see [Safety](#safety)); use only when certain.

Pass = write/verify PASS, SMART diff clean, temperature stayed within limits.

## Thermal pacing (passive enclosures)

**Passive (fanless) enclosures overheat on a sustained full write.** A continuous multi-TB write in a fanless USB enclosure climbs steadily past the drive's ~75-80 C throttle point, and the USB bridge eventually drops off the bus - which fails the test with no integrity result. Two mitigations, combine as needed:

- `--parts N` splits the write+verify into N regions, cooling the drive to the policy's `cool_target_c` before each, so heat never accumulates. Start around `--parts 8` for a passive enclosure. Before each region it also refuses to start if the drive is still hotter than `start_max_c` after cooling, and while a region runs it enforces the hard `ceiling_c`: if reached, `fio` is stopped cleanly (result `INCOMPLETE`) rather than riding into a disconnect.
- `--only SPEC` runs just some of the N parts, so you can break and resume without redoing everything. SPEC is a comma list of parts/ranges over the **same** `--parts N`, e.g. `--only 1-4` now and `--only 5-8` later (or `--only 6` to redo a single region). Always pass the same `--parts N` when resuming - region boundaries depend on it.
- A **fan** on the enclosure is the real fix - even a cheap desk fan drops it several degrees and may let a single `--write` pass complete.

Resuming across sessions: each `--only` run reports `PASS (parts X of N ...)` for just the parts it ran - the drive is fully verified only once every part has passed across your runs.

All the tunable defaults live in one place, the `config` module: `QUICK_BYTES` and `DEFAULT_PARTS`, plus `DEFAULT_THERMAL_POLICY` (the `ceiling_c` / `cool_target_c` / `start_max_c` / `cool_max_wait_s` / ... thresholds). They are the field defaults of `RunConfig`; the CLI resolves a `RunConfig` from them and the orchestrator consumes it, so this is the only place to adjust for a different drive or enclosure.

## Safety

`--write` is destructive. The tool refuses to touch the wrong disk through several independent guards:

- **One whole disk only** - a single `/dev` target, and it must be a whole disk, not a partition or dm/LVM/RAID/loop node (`TYPE != disk`).
- **Not mounted** - refuses if the disk or any child is mounted or in use as swap. Fails closed: if the state can't be read, it refuses.
- **Not the system disk** - refuses the disk backing `/`, walking through any LVM/RAID/LUKS/btrfs-subvolume layers. The target path is canonicalized first, so a `by-id`/`by-path` symlink can't slip past. If the root source can't be mapped to a disk (e.g. ZFS or overlay root), it warns rather than silently trusting.
- **Must be blank** - refuses `--write` if the disk has a partition table, filesystem/RAID/LVM signature, or kernel holders. Fails closed: if a blank-check probe errors, the disk is treated as non-blank. A brand-new drive is blank; anything found means you likely have the wrong disk. Override with `--force` only when certain.
- **Unique serial** - `--write` requires a non-empty serial that is unique among attached disks, so the pre-write identity check can reliably detect a node reassignment.
- **Serial confirmation** - `--write` prints the target's identity and requires typing its serial.
- **Re-check before writing** - the device identity (serial/WWN/size/model) and mount state are re-verified immediately before `fio` writes, so a replug that reassigns the node (e.g. `/dev/sdb` -> a different disk) aborts instead of wiping it.

Read-only mode (no `--write`) never writes and skips these write-only refusals.

Residual limits (inherent to any userspace tool): a disk with only a signature that `libblkid`/`wipefs` doesn't recognize can look blank; `--force` bypasses the blank guard (including on ZFS/overlay-root systems where the system-disk walk can't verify); and device-open is not atomic, so a hostile replug in the millisecond between the final re-check and `fio` remains theoretically possible. Always re-check the node before running `--write`.

## Notes

- Always re-check the device node after plugging in; `--write` erases the target.
- Long `fio` phases print a live progress line every 30s (percent + speed + ETA for the write pass; a time countdown for the read benchmarks). Full per-phase output is saved under the `drive_test_*` log folder.

## Development

Dependency-free `src/` layout package. The heavy logic (parsing, safety decisions, region math, result classification) is in pure functions, unit-tested against JSON fixtures captured from real hardware - no device needed to run the tests.

Modules, each useful on its own:

- `proc` - a thin, mockable subprocess seam (run a command, parse JSON).
- `tools` - check that required external CLIs are present.
- `units` - binary size constants (`KIB`/`MIB`/`GIB`).
- `config` - the resolved `RunConfig` and all default values (sizing + thermal policy).
- `devices` - enumerate block devices and model their identity (`lsblk`).
- `safety` - the destructive-write guards (all pure decisions over gathered data).
- `smart` - read SMART/health/temperature via `smartctl`/`nvme`.
- `thermal` - thermal pacing policy for passively-cooled enclosures.
- `planning` - split a drive into regions and parse `--only` specs.
- `fio` - build and run fio write+verify and read benchmarks.
- `probe` - gather the IO inputs (`wipefs`/`findmnt`/`/sys`) the guards consume.
- `report` - summary, SMART diff and result classification.
- `cli` / `orchestrator` - `cli` resolves flags into a `RunConfig`; `orchestrator` consumes it and runs the end-to-end battery.

The import graph is acyclic and layered; `pyproject.toml` encodes the layering as an `import-linter` contract.

Checks (all should be clean):

```bash
PYTHONPATH=src pytest -q     # unit + integration tests
ruff check src tests         # lint
pyright                      # types (strict for src; tests relaxed - see pyproject.toml)
PYTHONPATH=src uvx --from import-linter lint-imports   # layering / no import cycles
```

`pyproject.toml` defines a `drivetest` console script, so `pip install -e .` also exposes the `drivetest` command directly (the `./drivetest` wrapper just avoids needing an install).
