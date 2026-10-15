"""ASUS UX8406CA layout switch with revision checks and local recovery records.

Only the documented-in-this-project layout byte is backed up, not firmware.
No device is opened when importing this module or running --help.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

VID = 0x0B05
PID = 0x1BF2
REVISION = 0x0204
USAGE_PAGE = 0xFFE0
USAGE = 1
VALID_LAYOUTS = frozenset(range(1, 6)) | frozenset(range(0x81, 0x86))
SCRIPT_DIR = Path(__file__).resolve().parent
UPSTREAM_COMMIT = "cf992eb2a915da5436c01262169f2e8715ae4281"


class LayoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    vendor_id: int
    product_id: int
    revision: int
    usage_page: int
    usage: int
    input_report_length: int
    output_report_length: int
    serial: str = ""
    product: str = ""


def validate_device(info: DeviceInfo) -> None:
    if (info.vendor_id, info.product_id, info.usage_page, info.usage) != (
        VID, PID, USAGE_PAGE, USAGE
    ):
        raise LayoutError("Device is not the supported ASUS vendor interface.")
    if info.revision != REVISION:
        raise LayoutError(
            f"Unsupported device revision 0x{info.revision:04X}; required 0x0204. "
            "No configuration commands were sent."
        )
    if (info.input_report_length, info.output_report_length) != (64, 64):
        raise LayoutError("Unexpected HID report sizes; expected 64-byte input/output reports.")


def require_ack(reply: bytes, operation: str) -> None:
    if len(reply) < 6 or reply[4:6] != b"\xec\xac":
        raise LayoutError(f"{operation} rejected: {reply[:8].hex(' ')}")


@contextmanager
def configuration_session(transport):
    # Entry may reach the device even if its acknowledgement is lost. Always
    # attempt exit, including on entry timeout, invalid ACK or Ctrl+C.
    primary_error = None
    try:
        require_ack(transport.exchange(bytes.fromhex("a0 ea 5a a5")), "Entering configuration mode")
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            require_ack(transport.exchange(bytes.fromhex("a0 da 5a a5")), "Leaving configuration mode")
        except BaseException as cleanup_error:
            if primary_error is None:
                raise LayoutError(
                    f"Could not confirm leaving configuration mode: {cleanup_error}. "
                    "A layout change may already have succeeded; reconnect and check status."
                ) from cleanup_error
            primary_error.add_note(f"Configuration-mode cleanup also failed: {cleanup_error}")


def read_layout(transport) -> int:
    for delay in (0, 0.05, 0.1, 0.2, 0.4, 0.75, 1.25):
        if delay:
            time.sleep(delay)
        reply = transport.exchange(bytes.fromhex("c1 13"))
        if len(reply) < 6 or int.from_bytes(reply[2:4], "little") != 1 or reply[5] != 0:
            raise LayoutError(f"Invalid layout response: {reply[:8].hex(' ')}")
        raw = reply[4]
        if raw in VALID_LAYOUTS:
            return raw
        if raw != 0xFF:
            break
    raise LayoutError(f"Unsupported or unsettled layout value: 0x{raw:02X}")


def mode_name(raw: int) -> str:
    return "Copilot (Win + Shift + F23)" if raw & 0x80 else "Legacy (Right Ctrl)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Backups:
    def __init__(self, directory: Path):
        self.directory = directory

    def create(self, info: DeviceInfo, before: int, target: int, operation: str) -> tuple[Path, dict]:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.directory / f"layout-{stamp}-{uuid.uuid4().hex[:8]}.json"
        record = {
            "schema": 1,
            "kind": "asus-duo-layout-backup",
            "created_utc": utc_now(),
            "upstream_commit": UPSTREAM_COMMIT,
            "device": asdict(info),
            "original_layout": before,
            "requested_layout": target,
            "operation": operation,
            "state": "prepared",
            "scope": "One layout setting only; not a firmware image or full device backup.",
        }
        with path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if json.loads(path.read_text(encoding="utf-8")) != record:
            raise LayoutError("Backup read-back failed. Refusing to write the keyboard.")
        return path, record

    def update(self, path: Path, record: dict, **changes) -> None:
        updated = {**record, **changes, "updated_utc": utc_now()}
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(updated, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        record.clear()
        record.update(updated)


def load_backup(path: Path, info: DeviceInfo) -> int:
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema") != 1 or record.get("kind") != "asus-duo-layout-backup":
        raise LayoutError("Not a supported layout backup.")
    saved = record.get("device", {})
    for key in ("vendor_id", "product_id", "revision", "usage_page", "usage"):
        if saved.get(key) != getattr(info, key):
            raise LayoutError(f"Backup device mismatch: {key}.")
    if info.serial and saved.get("serial"):
        same_device = info.serial == saved["serial"]
    else:
        same_device = info.path.casefold() == str(saved.get("path", "")).casefold()
    if not same_device:
        raise LayoutError("Backup belongs to a different device or USB connection. Refusing restore.")
    original = record.get("original_layout")
    if type(original) is not int or original not in VALID_LAYOUTS:
        raise LayoutError("Backup contains an invalid original layout.")
    return original


def change_layout(transport, info: DeviceInfo, backups: Backups, *, copilot=None, restore=None):
    validate_device(info)  # Fail before even entering configuration mode.
    restored = load_backup(restore, info) if restore is not None else None
    if restore is None and type(copilot) is not bool:
        raise LayoutError("Specify a target mode.")
    backup_path = None
    record = None
    write_attempted = False
    try:
        with configuration_session(transport):
            before = read_layout(transport)
            target = restored if restored is not None else (before & 0x7F) | (0x80 if copilot else 0)
            if (before & 0x7F) != (target & 0x7F):
                raise LayoutError("Restore would change regional layout bits. Refusing to write.")
            if before == target:
                return {"changed": False, "layout": before, "backup": None}
            # A durable, readable backup and write-intent journal must both
            # succeed before the first persistent hardware write.
            backup_path, record = backups.create(info, before, target, "restore" if restore else "set")
            backups.update(backup_path, record, state="write_pending")
            print(f"Backup saved: {backup_path}", flush=True)
            write_attempted = True
            reply = transport.exchange(bytes([0xC1, 0x12, 1, 0, target]))
            require_ack(reply, "Saving layout")
            after = read_layout(transport)
            if after != target:
                raise LayoutError(f"Verification failed: expected 0x{target:02X}, read 0x{after:02X}.")
            backups.update(backup_path, record, state="verified", observed_layout=after)
        backups.update(backup_path, record, state="complete")
        return {"changed": True, "layout": target, "backup": str(backup_path)}
    except BaseException as error:
        if backup_path is not None:
            try:
                backups.update(backup_path, record, state="indeterminate" if write_attempted else "not_written", error=str(error))
            except BaseException as backup_error:
                error.add_note(f"Could not update backup journal: {backup_error}")
            error.add_note(f"Original layout backup: {backup_path}")
        if write_attempted:
            error.add_note("The device write may have succeeded. Check status before retrying; no automatic write retry or rollback was performed.")
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="Check revision and read the current layout; no persistent write")
    commands.add_parser("backup", help="Save the current layout without changing it")
    setter = commands.add_parser("set", help="Back up, change and verify the keyboard layout")
    setter.add_argument("mode", choices=("legacy", "copilot"))
    restore = commands.add_parser("restore", help="Back up the current state and restore an earlier layout backup")
    restore.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        from windows_hid import WindowsHid, find_keyboard
        info = find_keyboard()
        validate_device(info)
        print(f"Device: {info.product or 'ASUS keyboard'} ({info.vendor_id:04X}:{info.product_id:04X})")
        print(f"Reported USB/device revision: 0x{info.revision:04X} (required 0x0204)")
        backups = Backups(SCRIPT_DIR / "backups")
        with WindowsHid(info) as transport:
            if args.command in (None, "status", "backup"):
                with configuration_session(transport):
                    raw = read_layout(transport)
                    if args.command == "backup":
                        path, _ = backups.create(info, raw, raw, "backup")
                        print(f"Backup saved: {path}")
                print(f"Layout: 0x{raw:02X} - {mode_name(raw)}; regional/SKU ID {raw & 0x7F}")
            else:
                result = change_layout(transport, info, backups, copilot=args.mode == "copilot" if args.command == "set" else None, restore=args.file if args.command == "restore" else None)
                print(("Changed and verified: " if result["changed"] else "Already set; nothing written: ") + mode_name(result["layout"]))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"Error: {error or 'Interrupted'}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(f"  {note}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
