# Drive testing

Test a new **WD SN850X 2TB** SSD and **Asus ROG Strix Arion** USB-C enclosure before the SN850X replaces the system drive in the Dell XPS 13 9380.

The enclosure caps at ~1 GB/s (USB 3.2 Gen 2), so test in two stages: integrity/health externally now, real NVMe speed + the M.2 slot internally at swap time. The drive is new/empty, so destructive write tests are fine.

`drive_test.sh` runs the battery: SMART baseline -> optional full write+verify (crc32c) -> read benchmarks -> SMART diff -> pass/fail. Logs go to a timestamped folder.

## Stage 1 - external, via Arion enclosure

1. Plug in. Identify the node (appears as `/dev/sdX`):
   ```bash
   lsblk -o NAME,SIZE,MODEL,TRAN,SERIAL
   ```
2. Confirm 10 Gbps link (want `SuperSpeedPlus` / `10000M`):
   ```bash
   sudo dmesg | grep -iE 'usb.*(SuperSpeed|10000|5000)' | tail
   ```
3. Full destructive write/verify + health (wipes the drive; ~1.5-2.5 h):
   ```bash
   sudo ./drive_test.sh --write --parts 8 /dev/sdX
   ```
   Use `--quick` (first 50G) for a fast sanity pass first.

Pass = write/verify PASS, SMART diff clean, temp stayed under ~70 C.

**Passive enclosures overheat on a sustained full write.** A continuous 2 TB write in a fanless USB enclosure (e.g. the Arion) climbs steadily past the drive's ~75-80 C throttle point and the bridge eventually drops off the bus - which fails the test with no integrity result. Two mitigations, combine as needed:

- `--parts N` splits the write+verify into N regions with a cooldown between each, so heat never accumulates. Start with `--parts 8` for a passive enclosure. The script also enforces a hard temperature ceiling (78 C): if a region ever reaches it, fio is stopped cleanly (result `INCOMPLETE`) rather than riding into a disconnect.
- A **fan** on the enclosure is the real fix - even a cheap desk fan drops it several degrees and may let a single `--write` pass complete.

Because of this thermal limit, the definitive full-speed write+verify is really Stage 2 (internal); the external pass mainly screens for a dead-on-arrival drive.

## Stage 2 - internal, in the M.2 slot

Do at swap time. Fit the SN850X, boot a **Linux live USB**, drive is `/dev/nvme0n1`:

1. Native health + speed:
   ```bash
   sudo ./drive_test.sh --write /dev/nvme0n1   # expect ~7000 MB/s seq read
   ```
2. Built-in extended self-test (USB bridges can't pass this through):
   ```bash
   sudo nvme device-self-test /dev/nvme0n1 -s 2
   sudo nvme self-test-log /dev/nvme0n1
   ```

Confirms full performance and that the slot/contacts are good. Then clone/reinstall. **Keep the old drive untouched until Stage 2 passes.**

## Safety

`--write` is destructive. The script refuses to touch the wrong disk through several independent guards:

- **One whole disk only** - a single `/dev` target, and it must be a whole disk, not a partition or dm/LVM/RAID/loop node (`TYPE != disk`).
- **Not mounted** - refuses if the disk or any child is mounted or in use as swap. Fails closed: if the state can't be read, it refuses.
- **Not the system disk** - refuses the disk backing `/`, walking through any LVM/RAID/LUKS/btrfs-subvolume layers. The target path is canonicalized first, so a `by-id`/`by-path` symlink can't slip past. If the root source can't be mapped to a disk (e.g. ZFS or overlay root), it warns rather than silently trusting.
- **Must be blank** - refuses `--write` if the disk has a partition table, filesystem/RAID/LVM signature, or kernel holders. Fails closed: if a blank-check probe errors, the disk is treated as non-blank. A brand-new drive is blank; anything found means you likely have the wrong disk. Override with `--force` only when certain.
- **Unique serial** - `--write` requires a non-empty serial that is unique among attached disks, so the pre-write identity check can reliably detect a node reassignment.
- **Serial confirmation** - `--write` prints the target's identity and requires typing its serial.
- **Re-check before writing** - the device identity (serial/WWN/size/model) and mount state are re-verified immediately before `fio` writes, so a replug that reassigns the node (e.g. `/dev/sdb` -> a different disk) aborts instead of wiping it.

Read-only mode (no `--write`) never writes and skips these write-only refusals.

Residual limits (inherent, not fixable in a shell script): a disk with only a signature that `libblkid`/`wipefs` doesn't recognize can look blank; `--force` bypasses the blank guard (including on ZFS/overlay-root systems where the system-disk walk can't verify); and device-open is not atomic, so a hostile replug in the millisecond between the final re-check and `fio` remains theoretically possible. Always re-check the node before running `--write`.

## Notes

- Always re-check the device node after plugging in; `--write` erases the target.
- Long `fio` phases print a live progress line every 30s (percent + speed + ETA for the write pass; a time countdown for the read benchmarks). Full per-phase output is saved under the `drive_test_*` log folder.
