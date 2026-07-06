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

Despite the thermal limit, this external pass is the full-drive integrity check: `--parts 8` writes and crc-verifies every cell, just paced with cooldowns. What it can't measure is real NVMe speed (the enclosure caps at ~1 GB/s) - that, plus the M.2 slot itself, is what Stage 2 validates.

## Stage 2 - internal, in the M.2 slot

Do this at swap time. Fit the SN850X, boot a **Linux live USB**; the drive is now `/dev/nvme0n1`:

1. Native health + speed:
   ```bash
   sudo ./drivetest --quick --write /dev/nvme0n1   # slot + PCIe write path; expect ~7000 MB/s seq read
   ```
   Stage 1 already crc-verified a full write across every cell, and integrity is interface-independent (USB vs PCIe doesn't change what's stored). So this pass only needs to prove the things the enclosure couldn't: real NVMe speed and that the M.2 slot/contacts are good. `--quick` does that without spending a second full drive-write of endurance - the extended self-test below re-scans the whole media internally to cover the rest.

   Check in the output:
   - write/verify **PASS**, `err=0`, no `verify` mismatch.
   - seq read **~7000 MB/s** (a few thousand, not ~1000; ~1 GB/s means it negotiated a bad PCIe link / poor contact - reseat).
   - SMART diff clean: `Media and Data Integrity Errors: 0`, `Critical Warning: 0x00`, temp well under ~70 C.
2. Built-in extended self-test (USB bridges can't pass this through):
   ```bash
   sudo nvme device-self-test /dev/nvme0n1 -s 2
   sudo nvme self-test-log /dev/nvme0n1   # re-run to poll; takes minutes
   ```

   Check in `self-test-log`:
   - newest entry (`Self Test Result[0]`) reads **`Completed without error`** (result code `0`).
   - `Current operation: 0x0` = finished; a non-zero `% Completed` means still running - poll again.
   - any `Completed: ... failure` / non-zero result, or an `LBA`/`namespace` on a failed entry = bad media or slot; **abort the swap**.

This confirms full performance and that the slot/contacts are good. Then clone/reinstall. **Keep the old drive untouched until Stage 2 passes.**

## Real-world caveat: the Arion flaked mid-run

During a Stage 1 `--write --parts 8` run, the enclosure got disconnected and started flaking after successfully write-testing parts 1-5 of 8:

- On the initial USB port it dropped off the bus and then cycled endlessly (a stream of reconnects in `dmesg`).
- On another port it appeared in `lsblk` under the same name (`sda`) and looked fine in `dmesg`, but was invisible to `smartctl`.
- On yet another port it appeared in `lsblk` as `sdb`, SMART readable, but downgraded to low speed (`dmesg` confirmed a failed connect then reconnect in a different mode).

The web confirms a history of similar flakiness with this enclosure - but other available options may have this problem too.

The described effects were very stable on multiple attempts when using the same cable but could be different when connected with other cables or via different adapters (which is expected).

What didn't help:

- Unplugging the enclosure for a while.
- Removing the drive from enclosure, waiting, inserting back.

What helped:

- Laptop power off and on again then use the same port as was originally used - connected fine on the first attempt.

Possibly the host's xHCI/USB controller was latching - the "only works again after a reboot" pattern is a reported xHCI issue, not specific to this enclosure. A thing that could potentially help without rebooting (not tried) is rebinding the `xhci_hcd` PCI driver (`unbind` then `bind` under `/sys/bus/pci/drivers/xhci_hcd/`), which resets the whole controller.

As a potential preventive cure, disabling USB autosuspend for the xHCI is a commonly reported fix for these wedges. It can be done e.g. with the `usbcore.autosuspend=-1` kernel parameter or (more surgically) by applying `services.udev.extraRules` doing the same for a specific enclosure device.
