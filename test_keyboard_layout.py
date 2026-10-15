"""Every device and HID report in these tests is simulated."""
from dataclasses import replace
import ctypes
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import keyboard_layout as app
from windows_hid import WindowsHid

INFO = app.DeviceInfo("mock-device-path", app.VID, app.PID, app.REVISION, app.USAGE_PAGE, app.USAGE, 64, 64, "mock-serial", "Mock ASUS")
ENTER = bytes.fromhex("a0 ea 5a a5")
EXIT = bytes.fromhex("a0 da 5a a5")
READ = bytes.fromhex("c1 13")


class Keyboard:
    def __init__(self, raw=0x83):
        self.raw = raw
        self.commands = []
        self.enter_error = None
        self.exit_error = None
        self.write_error = None
        self.ignore_write = False
        self.before_write = lambda _: None

    def exchange(self, command):
        self.commands.append(command)
        if command == ENTER and self.enter_error:
            raise self.enter_error
        if command == EXIT and self.exit_error:
            raise self.exit_error
        if command == READ:
            return bytes([0xC1, 0x13, 1, 0, self.raw, 0])
        if command[:2] == bytes.fromhex("c1 12"):
            self.before_write(command)
            if not self.ignore_write:
                self.raw = command[4]
            if self.write_error:
                raise self.write_error
        return command[:2] + bytes.fromhex("00 00 ec ac")

    @property
    def writes(self):
        return [c for c in self.commands if c[:2] == bytes.fromhex("c1 12")]


class LayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.backups = app.Backups(Path(temporary.name) / "backups")
        self.keyboard = Keyboard()

    def records(self):
        return [json.loads(p.read_text()) for p in self.backups.directory.glob("*.json")]

    def change(self):
        return app.change_layout(self.keyboard, INFO, self.backups, copilot=False)

    def test_unsupported_revision_sends_no_commands(self):
        with self.assertRaisesRegex(app.LayoutError, "Unsupported device revision"):
            app.change_layout(self.keyboard, replace(INFO, revision=0x0203), self.backups, copilot=False)
        self.assertEqual([], self.keyboard.commands)

    def test_bad_identity_or_report_sizes_sends_no_commands(self):
        for info in (replace(INFO, product_id=1), replace(INFO, usage_page=1), replace(INFO, input_report_length=32)):
            with self.subTest(info=info), self.assertRaises(app.LayoutError):
                app.change_layout(self.keyboard, info, self.backups, copilot=False)
        self.assertEqual([], self.keyboard.commands)

    def test_lost_entry_ack_attempts_exit(self):
        self.keyboard.enter_error = TimeoutError("entry reply lost")
        with self.assertRaisesRegex(TimeoutError, "entry reply lost"):
            self.change()
        self.assertEqual([ENTER, EXIT], self.keyboard.commands)

    def test_primary_and_cleanup_errors_are_both_reported(self):
        self.keyboard.enter_error = TimeoutError("entry reply lost")
        self.keyboard.exit_error = TimeoutError("exit reply lost")
        with self.assertRaises(TimeoutError) as caught:
            self.change()
        self.assertIn("exit reply lost", " ".join(caught.exception.__notes__))

    def test_ctrl_c_still_attempts_exit(self):
        with self.assertRaises(KeyboardInterrupt):
            with app.configuration_session(self.keyboard):
                raise KeyboardInterrupt()
        self.assertEqual([ENTER, EXIT], self.keyboard.commands)

    def test_backup_failure_blocks_hardware_write(self):
        with patch.object(self.backups, "create", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.change()
        self.assertEqual([], self.keyboard.writes)
        self.assertEqual(EXIT, self.keyboard.commands[-1])

    def test_failed_journal_before_write_blocks_write(self):
        with patch.object(self.backups, "update", side_effect=OSError("read-only filesystem")):
            with self.assertRaises(OSError):
                self.change()
        self.assertEqual([], self.keyboard.writes)
        self.assertEqual(0x83, self.records()[0]["original_layout"])

    def test_backup_exists_before_write_and_region_survives(self):
        def inspect_backup(command):
            record = self.records()[0]
            self.assertEqual("write_pending", record["state"])
            self.assertEqual(0x83, record["original_layout"])
            self.assertEqual(0x03, record["requested_layout"])
            self.assertEqual(bytes.fromhex("c1 12 01 00 03"), command)
        self.keyboard.before_write = inspect_backup
        result = self.change()
        self.assertTrue(result["changed"])
        self.assertEqual(0x03, self.keyboard.raw)
        self.assertEqual("complete", self.records()[0]["state"])
        self.assertEqual(EXIT, self.keyboard.commands[-1])

    def test_noop_makes_no_backup_or_persistent_write(self):
        self.keyboard.raw = 3
        result = self.change()
        self.assertFalse(result["changed"])
        self.assertEqual([], self.keyboard.writes)
        self.assertFalse(self.backups.directory.exists())

    def test_write_timeout_is_not_retried_and_keeps_backup(self):
        self.keyboard.write_error = TimeoutError("write reply lost")
        with self.assertRaises(TimeoutError) as caught:
            self.change()
        self.assertEqual(1, len(self.keyboard.writes))
        self.assertEqual("indeterminate", self.records()[0]["state"])
        self.assertEqual(0x83, self.records()[0]["original_layout"])
        self.assertIn("may have succeeded", " ".join(caught.exception.__notes__))
        self.assertEqual(EXIT, self.keyboard.commands[-1])

    def test_readback_mismatch_is_not_success(self):
        self.keyboard.ignore_write = True
        with self.assertRaisesRegex(app.LayoutError, "Verification failed"):
            self.change()
        self.assertEqual("indeterminate", self.records()[0]["state"])
        self.assertEqual(1, len(self.keyboard.writes))

    def test_successful_write_with_failed_exit_is_indeterminate(self):
        self.keyboard.exit_error = TimeoutError("exit reply lost")
        with self.assertRaisesRegex(app.LayoutError, "Could not confirm leaving"):
            self.change()
        self.assertEqual(3, self.keyboard.raw)
        self.assertEqual("indeterminate", self.records()[0]["state"])

    def test_restore_creates_a_second_backup(self):
        first = self.change()
        result = app.change_layout(self.keyboard, INFO, self.backups, restore=Path(first["backup"]))
        self.assertEqual(0x83, result["layout"])
        records = self.records()
        self.assertEqual(2, len(records))
        restored = next(r for r in records if r["operation"] == "restore")
        self.assertEqual(3, restored["original_layout"])

    def test_backup_identity_mismatch_blocks_all_commands(self):
        path, _ = self.backups.create(INFO, 3, 3, "backup")
        other = replace(INFO, serial="different-keyboard")
        with self.assertRaisesRegex(app.LayoutError, "different device"):
            app.change_layout(self.keyboard, other, self.backups, restore=path)
        self.assertEqual([], self.keyboard.commands)

    def test_restore_cannot_change_regional_layout(self):
        path, _ = self.backups.create(INFO, 4, 4, "backup")
        with self.assertRaisesRegex(app.LayoutError, "regional layout bits"):
            app.change_layout(self.keyboard, INFO, self.backups, restore=path)
        self.assertEqual([], self.keyboard.writes)
        self.assertEqual(EXIT, self.keyboard.commands[-1])

    def test_invalid_read_still_cleans_up(self):
        self.keyboard.raw = 0x7F
        with self.assertRaisesRegex(app.LayoutError, "Unsupported or unsettled"):
            self.change()
        self.assertEqual([], self.keyboard.writes)
        self.assertEqual(EXIT, self.keyboard.commands[-1])

    def test_journal_failure_after_write_retains_original_backup(self):
        update = self.backups.update
        def fail_after_write(path, record, **changes):
            if changes.get("state") == "verified":
                raise OSError("disk became unavailable")
            return update(path, record, **changes)
        with patch.object(self.backups, "update", side_effect=fail_after_write):
            with self.assertRaisesRegex(OSError, "disk became unavailable"):
                self.change()
        self.assertEqual(3, self.keyboard.raw)
        self.assertEqual(0x83, self.records()[0]["original_layout"])
        self.assertEqual("indeterminate", self.records()[0]["state"])
        self.assertEqual(EXIT, self.keyboard.commands[-1])


class ReportTests(unittest.TestCase):
    def test_pending_native_read_is_cancelled_on_timeout_or_interrupt(self):
        for interruption in ("none", "wait", "start"):
            with self.subTest(interruption=interruption):
                calls = []
                def read(*_):
                    if interruption == "start":
                        raise KeyboardInterrupt()
                    ctypes.set_last_error(997)
                    return False
                def wait(*_):
                    if interruption == "wait":
                        raise KeyboardInterrupt()
                    return 258
                def cancel(*_):
                    calls.append("cancel")
                    return True
                def finish(*args):
                    calls.append("finish-cancel")
                    self.assertTrue(args[-1])
                    return False
                def close(*_):
                    calls.append("close-event")
                    return True
                kernel = SimpleNamespace(CreateEventW=lambda *_: 99, ReadFile=read,
                                         WaitForSingleObject=wait, CancelIoEx=cancel,
                                         GetOverlappedResult=finish, CloseHandle=close)
                transport = WindowsHid.__new__(WindowsHid)
                transport.handle = 1
                transport.api = SimpleNamespace(kernel=kernel)
                expected = TimeoutError if interruption == "none" else KeyboardInterrupt
                with self.assertRaises(expected):
                    transport._transfer("ReadFile", ctypes.create_string_buffer(64), time.monotonic() + 1)
                self.assertEqual(["cancel", "finish-cancel", "close-event"], calls)

    def test_changed_revision_before_open_releases_mutex_and_never_opens_reports(self):
        calls = []
        kernel = SimpleNamespace(CreateMutexW=lambda *_: 99, WaitForSingleObject=lambda *_: 0,
                                 ReleaseMutex=lambda *_: calls.append("release-mutex"),
                                 CloseHandle=lambda *_: calls.append("close-mutex"),
                                 CreateFileW=lambda *_: self.fail("Report I/O must not open"))
        transport = WindowsHid.__new__(WindowsHid)
        transport.info = INFO
        transport.handle = None
        transport.mutex = None
        transport.owns_mutex = False
        transport.api = SimpleNamespace(kernel=kernel, describe=lambda _: replace(INFO, revision=0x0205))
        with self.assertRaisesRegex(app.LayoutError, "Unsupported device revision"):
            transport.__enter__()
        self.assertEqual(["release-mutex", "close-mutex"], calls)

    def test_report_framing_and_unrelated_responses(self):
        transport = WindowsHid.__new__(WindowsHid)
        transport.handle = 1  # fake; _transfer below never calls Windows
        commands = []
        responses = iter([bytes.fromhex("21 a0 ea 00 00 ec ac"), bytes.fromhex("21 c1 13 01 00 83 00")])
        def transfer(operation, buffer, deadline):
            if operation == "WriteFile":
                commands.append(buffer.raw)
                return 64
            response = next(responses)
            buffer.raw = response.ljust(64, b"\0")
            return 64
        transport._transfer = transfer
        self.assertEqual(0x83, transport.exchange(READ)[4])
        self.assertEqual(1, len(commands))
        self.assertEqual(bytes.fromhex("20 c1 13") + bytes(61), commands[0])

    def test_report_timeout_does_not_repeat_write(self):
        transport = WindowsHid.__new__(WindowsHid)
        transport.handle = 1
        operations = []
        def transfer(operation, buffer, deadline):
            operations.append(operation)
            if operation == "ReadFile":
                raise TimeoutError("simulated timeout")
            return 64
        transport._transfer = transfer
        with self.assertRaises(TimeoutError):
            transport.exchange(bytes.fromhex("c1 12 01 00 03"))
        self.assertEqual(["WriteFile", "ReadFile"], operations)


if __name__ == "__main__":
    unittest.main()
