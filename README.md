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
   sudo ./drive_test.sh --write /dev/sdX
   ```
   Use `--quick` (first 50G) for a fast sanity pass first.

Pass = write/verify PASS, SMART diff clean, temp stayed under ~70 C.

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

- **Whole-disk only** - rejects partitions and dm/LVM/RAID/loop nodes (`TYPE != disk`).
- **Not mounted** - refuses if the disk or any partition is mounted or in use as swap.
- **Not the system disk** - refuses the disk backing `/`. The target path is canonicalized first, so a `by-id`/`by-path` symlink can't slip past this.
- **Must be blank** - refuses `--write` if the disk has a partition table, filesystem/RAID/LVM signature, or kernel holders. A brand-new drive is blank; anything found means you likely have the wrong disk. Override with `--force` only when certain.
- **Serial confirmation** - `--write` prints the target's identity and requires typing its serial.
- **Re-check before writing** - the device identity (serial/WWN/size) and mount state are re-verified immediately before `fio` writes, so a replug that reassigns the node (e.g. `/dev/sdb` -> a different disk) aborts instead of wiping it.

Read-only mode (no `--write`) never writes and skips these write-only refusals.

## Notes

- Always re-check the device node after plugging in; `--write` erases the target.
