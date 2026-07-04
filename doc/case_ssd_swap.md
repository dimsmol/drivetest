# Case: WD SN850X external screening + M.2 swap (Dell XPS 13 9380)

A worked, real-world example of using `drivetest`. See the [README](../README.md) for the general tool, its flags, and the safety model.

## Goal

Test a new **WD SN850X 2TB** SSD and an **Asus ROG Strix Arion** USB-C enclosure before the SN850X replaces the system drive in a **Dell XPS 13 9380**.

The Arion enclosure caps at ~1 GB/s (USB 3.2 Gen 2), so testing splits into two stages: integrity/health externally now, real NVMe speed + the M.2 slot internally at swap time. The drive is new/empty, so destructive write tests are fine.

## Stage 1 - external, via the Arion enclosure

1. Plug in. Identify the node (appears as `/dev/sdX`):
   ```bash
   lsblk -o NAME,SIZE,MODEL,TRAN,SERIAL
   ```
2. Confirm the 10 Gbps link (want `SuperSpeedPlus` / `10000M`):
   ```bash
   sudo dmesg | grep -iE 'usb.*(SuperSpeed|10000|5000)' | tail
   ```
3. Full destructive write/verify + health (wipes the drive; ~1.5-2.5 h):
   ```bash
   sudo ./drivetest --write --parts 8 /dev/sdX
   ```
   Use `--quick` (first 50 GiB) for a fast sanity pass first.

Pass = write/verify PASS, SMART diff clean, temp stayed under ~70 C.

The Arion is a passive (fanless) enclosure, so a single continuous 2 TB write overheats and the bridge drops off the bus. `--parts 8` paces the write with cooldowns between regions; see [Thermal pacing](../README.md#thermal-pacing-passive-enclosures) in the README. A desk fan on the enclosure helps a lot, and may even let a single un-split `--write` complete.

Because of this thermal limit, the definitive full-speed write+verify is really Stage 2 (internal); the external pass mainly screens for a dead-on-arrival drive.

## Stage 2 - internal, in the M.2 slot

Do this at swap time. Fit the SN850X, boot a **Linux live USB**; the drive is now `/dev/nvme0n1`:

1. Native health + speed:
   ```bash
   sudo ./drivetest --write /dev/nvme0n1   # expect ~7000 MB/s seq read
   ```
2. Built-in extended self-test (USB bridges can't pass this through):
   ```bash
   sudo nvme device-self-test /dev/nvme0n1 -s 2
   sudo nvme self-test-log /dev/nvme0n1
   ```

This confirms full performance and that the slot/contacts are good. Then clone/reinstall. **Keep the old drive untouched until Stage 2 passes.**
