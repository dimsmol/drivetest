#!/usr/bin/env bash
#
# drive_test.sh - health, integrity and performance test for a new SSD.
#
# Works both for a drive in a USB enclosure (shows up as /dev/sdX) and for an
# NVMe drive in the M.2 slot (/dev/nvmeXn1). Resolves the target by model and
# serial, refuses to touch a mounted or system disk, snapshots SMART before and
# after, and diffs the two so error/reallocation counts are obvious.
#
# Read-only by default. Pass --write to run the destructive full-surface
# write+verify pass (this WIPES the target - only for a new/empty drive).
#
set -euo pipefail

# --- options ---------------------------------------------------------------

DO_WRITE=0
QUICK=0
FORCE=0
DEV=""

usage() {
  cat <<'EOF'
drive_test.sh - health, integrity and performance test for an SSD.

Usage:
  sudo ./drive_test.sh [--write] [--quick] [--force] /dev/DEVICE

  (no flags)  SMART health + read benchmarks only (non-destructive)
  --write     also run the full destructive write+verify pass (WIPES target)
  --quick     with --write, verify only the first 50G
  --force     allow --write to a non-blank disk (has partitions/signatures)

Examples:
  sudo ./drive_test.sh /dev/sdb
  sudo ./drive_test.sh --write /dev/sdb
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --write)  DO_WRITE=1 ;;
    --quick)  QUICK=1 ;;
    --force)  FORCE=1 ;;
    -h|--help) usage 0 ;;
    /dev/*)   [[ -z "$DEV" ]] || { echo "error: more than one device given ($DEV, $1)" >&2; usage 1; }
              DEV="$1" ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
  shift
done

[[ -n "$DEV" ]] || { echo "error: no /dev/... target given" >&2; usage 1; }
[[ $EUID -eq 0 ]] || { echo "error: run as root (sudo)" >&2; exit 1; }

# Canonicalize early: a by-id/by-path symlink must resolve to the real /dev node
# so every guard and comparison below sees the same path (a symlink would slip
# past the string-compare system-disk guard otherwise).
DEV="$(readlink -f -- "$DEV")"
[[ -b "$DEV" ]] || { echo "error: $DEV is not a block device" >&2; exit 1; }

# Check every external tool the script relies on, up front, and report all that
# are missing at once. nvme-cli is only needed for an NVMe target.
REQUIRED=(smartctl lsblk findmnt fio wipefs awk grep sed sort diff tail tee date mkdir readlink)
[[ "$DEV" == *nvme* ]] && REQUIRED+=(nvme)
missing=()
for t in "${REQUIRED[@]}"; do
  command -v "$t" >/dev/null 2>&1 || missing+=("$t")
done
if (( ${#missing[@]} )); then
  echo "error: missing required tools: ${missing[*]}" >&2
  echo "install them, e.g.: nix-shell -p fio smartmontools nvme-cli usbutils" >&2
  exit 1
fi

# --- safety checks ---------------------------------------------------------

# Must be a whole disk, not a partition.
[[ "$(lsblk -dno TYPE "$DEV")" == "disk" ]] || {
  echo "error: $DEV is not a whole disk (looks like a partition)" >&2; exit 1; }

# True if the disk (or any child) is mounted/swap, OR if its state cannot be
# read. Fails CLOSED: an lsblk error counts as "unsafe" so we never write when
# we cannot positively confirm the device is idle.
is_mounted() {
  local mnt
  mnt="$(lsblk -nro MOUNTPOINT "$DEV" 2>/dev/null)" || return 0
  [[ -n "${mnt//[[:space:]]/}" ]]
}

# Refuse if the device or any of its partitions is mounted.
if is_mounted; then
  echo "error: $DEV is mounted, or its state could not be read - unmount first" >&2
  lsblk "$DEV" 2>/dev/null || true; exit 1
fi

# Refuse if this disk backs / (through any LVM/RAID/LUKS layers). lsblk -s walks
# from the root source down to its physical parent disk(s), so this holds even
# when / is on dm-crypt, LVM, or a multi-disk RAID.
ROOT_SRC="$(findmnt -no SOURCE / 2>/dev/null || true)"
ROOT_SRC="${ROOT_SRC%%[*}"   # strip a btrfs subvolume suffix like "[/@root]"
if [[ "$ROOT_SRC" == /dev/* ]]; then
  while read -r n; do
    [[ "/dev/$n" == "$DEV" ]] || continue
    echo "error: $DEV backs the running system (/). Refusing." >&2; exit 1
  done < <(lsblk -nrso NAME "$ROOT_SRC" 2>/dev/null || true)
elif [[ -n "$ROOT_SRC" ]]; then
  # Root source is not a plain block device (e.g. ZFS dataset, overlay, network
  # root), so we cannot map it to a disk to compare. Warn rather than silently
  # trusting - the blank-disk guard below is then the main backstop.
  echo "warning: cannot resolve the disk backing / (root source: $ROOT_SRC)." >&2
  echo "         Make sure $DEV is not part of your system storage." >&2
fi

# Refuse to WRITE to a disk that isn't blank, unless --force. A brand-new drive
# has no partition table, filesystem, or RAID/LVM signature, and no kernel
# holders; anything found here strongly suggests the wrong disk was given (an
# idle data disk can pass the mount/system-disk guards above).
DEVNAME="${DEV##*/}"
# Probe for content. Fail CLOSED: if any probe errors (device busy/vanished,
# quirky bridge), treat the disk as non-blank so --write refuses rather than
# proceeding on the false impression that the disk is empty.
PROBE_ERR=0
HOLDERS="$(ls -A "/sys/block/$DEVNAME/holders" 2>/dev/null)" || PROBE_ERR=1
SIGS="$(wipefs -n -- "$DEV" 2>/dev/null | tail -n +2)" || PROBE_ERR=1
CHILDREN="$(lsblk -nro NAME "$DEV" 2>/dev/null | tail -n +2)" || PROBE_ERR=1
if [[ $PROBE_ERR == 1 || -n "$HOLDERS" || -n "$SIGS" || -n "$CHILDREN" ]]; then
  echo "warning: $DEV is not blank, or its content could not be fully read:" >&2
  [[ $PROBE_ERR == 1 ]] && echo "  (a blank-check probe failed - treating as non-blank)" >&2
  [[ -n "$CHILDREN" ]]  && lsblk "$DEV" >&2
  [[ -n "$SIGS" ]]      && { echo "  signatures:" >&2; wipefs -n -- "$DEV" >&2; }
  [[ -n "$HOLDERS" ]]   && echo "  in use by (holders): $HOLDERS" >&2
  if [[ $DO_WRITE == 1 && $FORCE == 0 ]]; then
    echo "error: refusing --write to a non-blank disk. If you are certain this" >&2
    echo "       is the right (new) drive, re-run with --force." >&2
    exit 1
  fi
fi

# --- device identity + smartctl access mode -------------------------------

MODEL="$(lsblk -dno MODEL "$DEV" | xargs || true)"
SERIAL="$(lsblk -dno SERIAL "$DEV" | xargs || true)"
SIZE="$(lsblk -dno SIZE "$DEV")"
TRAN="$(lsblk -dno TRAN "$DEV" | xargs || true)"

# Stable identity fingerprint, used to detect the node being reassigned to a
# different disk (e.g. an enclosure replug) between confirmation and write.
dev_ident() { lsblk -dno SERIAL,WWN,SIZE,MODEL "$DEV" 2>/dev/null | xargs || true; }
IDENT="$(dev_ident)"

# Find the smartctl -d args that actually work for this device (bare, then the
# common USB-NVMe bridge modes, then SAT for SATA-behind-USB).
SMART_ARGS=()
for a in "" "-d nvme" "-d sntasmedia" "-d sntrealtek" "-d sat"; do
  read -ra cand <<<"$a"
  if smartctl -i "${cand[@]}" "$DEV" >/dev/null 2>&1; then
    SMART_ARGS=("${cand[@]}"); break
  fi
done

# --- logging ---------------------------------------------------------------

STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="drive_test_${SERIAL:-unknown}_${STAMP}"
mkdir -p "$LOG_DIR"
SUMMARY="$LOG_DIR/summary.log"

log() { echo "$@" | tee -a "$SUMMARY"; }

log "== drive_test $STAMP =="
log "device : $DEV"
log "model  : $MODEL"
log "serial : $SERIAL"
log "size   : $SIZE"
log "bus    : $TRAN"
log "smart  : smartctl ${SMART_ARGS[*]:-(auto)}"
log "logs   : $LOG_DIR/"
log "mode   : $([[ $DO_WRITE == 1 ]] && echo 'READ + DESTRUCTIVE WRITE/VERIFY' || echo 'read-only')"
log ""

# --- confirmation ----------------------------------------------------------

if [[ $DO_WRITE == 1 ]]; then
  # The pre-write identity re-check can only detect a node reassignment (e.g. a
  # hotplug that hands $DEV to a different disk) if the serial uniquely names
  # this disk. Require a non-empty serial that is unique among attached disks;
  # cheap USB bridges sometimes report a fixed or duplicate serial.
  if [[ -z "$SERIAL" ]]; then
    echo "error: $DEV reports no serial - refusing --write (identity unverifiable)." >&2; exit 1
  fi
  nser="$(lsblk -dno SERIAL 2>/dev/null | sed 's/[[:space:]]*$//' | grep -Fxc -- "$SERIAL" || true)"
  if [[ "${nser:-0}" -gt 1 ]]; then
    echo "error: serial '$SERIAL' is not unique among attached disks ($nser matches)." >&2
    echo "       Detach the other device; the pre-write identity check is unreliable otherwise." >&2
    exit 1
  fi
  echo "*** WRITE mode will ERASE ALL DATA on:"
  echo "      $DEV  |  $MODEL  |  serial $SERIAL  |  $SIZE  |  bus ${TRAN:-?}"
  { [[ -n "$CHILDREN$SIGS$HOLDERS" || $PROBE_ERR == 1 ]] \
      && echo "    NOTE: this disk is NOT blank / not fully readable (see warning above) ***"; } \
    || echo "***"
  read -rp "Type the serial ($SERIAL) to confirm: " ans
  [[ "$ans" == "$SERIAL" && -n "$SERIAL" ]] || { echo "aborted (serial mismatch or empty)."; exit 1; }
fi

# --- helpers ---------------------------------------------------------------

# Best-effort current temperature in Celsius (integer) or "n/a".
get_temp() {
  local t
  if [[ "$DEV" == *nvme* ]] && command -v nvme >/dev/null; then
    t="$(nvme smart-log "$DEV" 2>/dev/null | awk -F: '/^temperature/{print $2; exit}')"
  else
    t="$(smartctl -A "${SMART_ARGS[@]}" "$DEV" 2>/dev/null | awk '/Temperature/{print; exit}')"
  fi
  t="$(grep -oE '[0-9]+' <<<"${t:-}" | awk '$1>=15 && $1<=110 {print; exit}')"
  echo "${t:-n/a}"
}

TEMP_LOG="$LOG_DIR/temperature.log"
MON_PID=""
start_temp_monitor() {
  ( while true; do echo "$(date +%H:%M:%S) $(get_temp)"; sleep 5; done ) \
    >>"$TEMP_LOG" 2>/dev/null &
  MON_PID=$!
}
stop_temp_monitor() { [[ -n "$MON_PID" ]] && kill "$MON_PID" 2>/dev/null || true; MON_PID=""; }
peak_temp() {
  [[ -f "$TEMP_LOG" ]] || { echo "n/a"; return; }
  awk '{print $2}' "$TEMP_LOG" | grep -oE '^[0-9]+$' | sort -n | tail -1 || echo "n/a"
}
trap 'stop_temp_monitor' EXIT

# fio progress: emit an ETA/throughput line every 30s. --eta=always forces it
# even though stdout is piped through tee (fio would otherwise stay silent until
# done); --eta-newline prints full lines (not in-place \r) so tee passes them on.
ETA_OPTS=(--eta=always --eta-newline=30s)

# --- 1. SMART baseline -----------------------------------------------------

log ">> SMART baseline -> $LOG_DIR/smart_before.txt"
smartctl -x "${SMART_ARGS[@]}" "$DEV" >"$LOG_DIR/smart_before.txt" 2>&1 || true
log "   health: $(smartctl -H "${SMART_ARGS[@]}" "$DEV" 2>/dev/null | grep -iE 'result|health' || echo '?')"
log "   temp  : $(get_temp) C"
log ""

# --- 2. destructive write + verify (optional) ------------------------------

VERIFY_OK="skipped"
if [[ $DO_WRITE == 1 ]]; then
  # Last line of defense against a node reassigned since we confirmed: the disk
  # must still be the same physical device, and still unmounted.
  if [[ "$(dev_ident)" != "$IDENT" ]]; then
    log "error: $DEV identity changed since confirmation - aborting write."
    log "       was: [$IDENT]  now: [$(dev_ident)]"; exit 1
  fi
  if is_mounted; then
    log "error: $DEV became mounted or unreadable - aborting write."; exit 1
  fi
  SIZE_ARG=$([[ $QUICK == 1 ]] && echo "--size=50G" || echo "--size=100%")
  log ">> write+verify (crc32c, $([[ $QUICK == 1 ]] && echo '50G' || echo 'full')) - this is the long one"
  start_temp_monitor
  if fio --name=writeverify --filename="$DEV" --ioengine=libaio --direct=1 \
        --bs=1M --iodepth=16 --rw=write --verify=crc32c --do_verify=1 \
        --verify_fatal=1 --verify_state_save=0 "$SIZE_ARG" --group_reporting "${ETA_OPTS[@]}" \
        2>&1 | tee "$LOG_DIR/fio_writeverify.log"; then
    VERIFY_OK="PASS"
  else
    VERIFY_OK="FAIL"
  fi
  stop_temp_monitor
  log "   write/verify: $VERIFY_OK"
  log ""
fi

# --- 3. read benchmarks (non-destructive) ----------------------------------

log ">> sequential read (1M, qd32, 60s)"
fio --name=seqread --filename="$DEV" --ioengine=libaio --direct=1 --bs=1M \
    --iodepth=32 --rw=read --runtime=60 --time_based --size=100% \
    --group_reporting "${ETA_OPTS[@]}" 2>&1 | tee "$LOG_DIR/fio_seqread.log" || true
grep -E 'READ: bw=' "$LOG_DIR/fio_seqread.log" | head -1 | sed 's/^/  /' | tee -a "$SUMMARY" || true

log ">> random read (4k, qd64, 30s)"
fio --name=randread --filename="$DEV" --ioengine=libaio --direct=1 --bs=4k \
    --iodepth=64 --rw=randread --runtime=30 --time_based --size=100% \
    --group_reporting "${ETA_OPTS[@]}" 2>&1 | tee "$LOG_DIR/fio_randread.log" || true
grep -iE 'read:.*IOPS' "$LOG_DIR/fio_randread.log" | head -1 | sed 's/^/   /' | tee -a "$SUMMARY" || true
log ""

# --- 4. SMART post + diff --------------------------------------------------

log ">> SMART after -> $LOG_DIR/smart_after.txt"
smartctl -x "${SMART_ARGS[@]}" "$DEV" >"$LOG_DIR/smart_after.txt" 2>&1 || true

# Strip volatile lines (temps, timestamps, power-on) before diffing.
scrub() { grep -viE 'temperature|power_on|power on|local time|data units|host (read|writ)|number of hours|percentage used' "$1"; }
diff <(scrub "$LOG_DIR/smart_before.txt") <(scrub "$LOG_DIR/smart_after.txt") \
    >"$LOG_DIR/smart_diff.txt" || true

SMART_FLAG="clean"
if grep -qiE 'reallocat|pending|uncorrect|media.error|crc.error|error (count|log)' \
     "$LOG_DIR/smart_diff.txt"; then
  SMART_FLAG="CHANGED - review smart_diff.txt"
fi

# --- summary ---------------------------------------------------------------

log ""
log "== summary =="
log "write/verify : $VERIFY_OK"
log "peak temp    : $(peak_temp) C"
log "SMART diff   : $SMART_FLAG"
log "full logs    : $LOG_DIR/"

if [[ "$VERIFY_OK" == "FAIL" || "$SMART_FLAG" != "clean" ]]; then
  log ""
  log "RESULT: ATTENTION NEEDED - inspect logs above."
  exit 2
fi
log ""
log "RESULT: OK"
