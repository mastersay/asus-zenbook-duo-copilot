"""Windows HID transport using only the Python standard library.

Descriptor enumeration sends no vendor/configuration reports. Real HID I/O
occurs only when the CLI is run and explicitly enters a WindowsHid context.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
import time

from keyboard_layout import DeviceInfo, LayoutError, PID, USAGE, USAGE_PAGE, VID, validate_device


class GUID(C.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", C.c_ubyte * 8)]


class InterfaceData(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("guid", GUID), ("flags", W.DWORD), ("reserved", C.c_void_p)]


class Attributes(C.Structure):
    _fields_ = [("Size", W.DWORD), ("VendorID", W.WORD), ("ProductID", W.WORD), ("VersionNumber", W.WORD)]


class Overlapped(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t), ("Offset", W.DWORD), ("OffsetHigh", W.DWORD), ("hEvent", W.HANDLE)]


INVALID_HANDLE = C.c_void_p(-1).value


class Api:
    def __init__(self):
        if os.name != "nt":
            raise LayoutError("The local runner requires Windows and Python 3.11 or newer.")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.setup = C.WinDLL("setupapi", use_last_error=True)
        self.hid = C.WinDLL("hid", use_last_error=True)
        self._bind(self.kernel, "CreateFileW", W.HANDLE, [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE])
        self._bind(self.kernel, "CloseHandle", W.BOOL, [W.HANDLE])
        self._bind(self.kernel, "CreateEventW", W.HANDLE, [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR])
        self._bind(self.kernel, "CreateMutexW", W.HANDLE, [C.c_void_p, W.BOOL, W.LPCWSTR])
        self._bind(self.kernel, "ReleaseMutex", W.BOOL, [W.HANDLE])
        self._bind(self.kernel, "WaitForSingleObject", W.DWORD, [W.HANDLE, W.DWORD])
        for name in ("ReadFile", "WriteFile"):
            self._bind(self.kernel, name, W.BOOL, [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.POINTER(Overlapped)])
        self._bind(self.kernel, "GetOverlappedResult", W.BOOL, [W.HANDLE, C.POINTER(Overlapped), C.POINTER(W.DWORD), W.BOOL])
        self._bind(self.kernel, "CancelIoEx", W.BOOL, [W.HANDLE, C.POINTER(Overlapped)])
        self._bind(self.hid, "HidD_GetHidGuid", None, [C.POINTER(GUID)])
        self._bind(self.hid, "HidD_GetAttributes", W.BOOLEAN, [W.HANDLE, C.POINTER(Attributes)])
        self._bind(self.hid, "HidD_GetPreparsedData", W.BOOLEAN, [W.HANDLE, C.POINTER(C.c_void_p)])
        self._bind(self.hid, "HidD_FreePreparsedData", W.BOOLEAN, [C.c_void_p])
        self._bind(self.hid, "HidP_GetCaps", C.c_long, [C.c_void_p, C.c_void_p])
        for name in ("HidD_GetProductString", "HidD_GetSerialNumberString"):
            self._bind(self.hid, name, W.BOOLEAN, [W.HANDLE, C.c_void_p, W.ULONG])
        self._bind(self.setup, "SetupDiGetClassDevsW", W.HANDLE, [C.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD])
        self._bind(self.setup, "SetupDiEnumDeviceInterfaces", W.BOOL, [W.HANDLE, C.c_void_p, C.POINTER(GUID), W.DWORD, C.POINTER(InterfaceData)])
        self._bind(self.setup, "SetupDiGetDeviceInterfaceDetailW", W.BOOL, [W.HANDLE, C.POINTER(InterfaceData), C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p])
        self._bind(self.setup, "SetupDiDestroyDeviceInfoList", W.BOOL, [W.HANDLE])

    @staticmethod
    def _bind(library, name, result, args):
        function = getattr(library, name)
        function.restype = result
        function.argtypes = args

    def string(self, handle, name):
        buffer = C.create_unicode_buffer(128)
        return buffer.value if getattr(self.hid, name)(handle, buffer, C.sizeof(buffer)) else ""

    def describe(self, path):
        # Access=0 reads USB/HID metadata without opening report I/O.
        handle = self.kernel.CreateFileW(path, 0, 3, None, 3, 0, None)
        if handle == INVALID_HANDLE:
            return None
        try:
            attributes = Attributes()
            attributes.Size = C.sizeof(attributes)
            if not self.hid.HidD_GetAttributes(handle, C.byref(attributes)):
                return None
            if (attributes.VendorID, attributes.ProductID) != (VID, PID):
                return None
            preparsed = C.c_void_p()
            if not self.hid.HidD_GetPreparsedData(handle, C.byref(preparsed)):
                return None
            try:
                caps = (W.WORD * 32)()  # HIDP_CAPS consists of 32 USHORT fields.
                if self.hid.HidP_GetCaps(preparsed, caps) != 0x00110000:
                    return None
            finally:
                self.hid.HidD_FreePreparsedData(preparsed)
            if (caps[1], caps[0]) != (USAGE_PAGE, USAGE):
                return None
            return DeviceInfo(
                path=path, vendor_id=attributes.VendorID, product_id=attributes.ProductID,
                revision=attributes.VersionNumber, usage_page=caps[1], usage=caps[0],
                input_report_length=caps[2], output_report_length=caps[3],
                serial=self.string(handle, "HidD_GetSerialNumberString"),
                product=self.string(handle, "HidD_GetProductString"),
            )
        finally:
            self.kernel.CloseHandle(handle)

    def devices(self):
        guid = GUID()
        self.hid.HidD_GetHidGuid(C.byref(guid))
        device_set = self.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)
        if device_set == INVALID_HANDLE:
            raise C.WinError(C.get_last_error())
        try:
            index = 0
            while True:
                interface = InterfaceData()
                interface.cbSize = C.sizeof(interface)
                if not self.setup.SetupDiEnumDeviceInterfaces(device_set, None, C.byref(guid), index, C.byref(interface)):
                    if C.get_last_error() == 259:  # ERROR_NO_MORE_ITEMS
                        break
                    raise C.WinError(C.get_last_error())
                index += 1
                required = W.DWORD()
                self.setup.SetupDiGetDeviceInterfaceDetailW(device_set, C.byref(interface), None, 0, C.byref(required), None)
                if required.value < 8:
                    raise LayoutError("Windows returned an invalid HID device-path size.")
                detail = C.create_string_buffer(required.value)
                C.cast(detail, C.POINTER(W.DWORD))[0] = 8 if C.sizeof(C.c_void_p) == 8 else 6
                if not self.setup.SetupDiGetDeviceInterfaceDetailW(device_set, C.byref(interface), detail, required, None, None):
                    raise C.WinError(C.get_last_error())
                path = C.wstring_at(C.addressof(detail) + 4)
                info = self.describe(path)
                if info is not None:
                    yield info
        finally:
            self.setup.SetupDiDestroyDeviceInfoList(device_set)


def find_keyboard():
    devices = list(Api().devices())
    if not devices:
        raise LayoutError("Supported ASUS USB keyboard interface not found. Connect the keyboard directly by USB.")
    if len(devices) != 1:
        raise LayoutError("Multiple matching ASUS keyboards found. Connect only the keyboard to change.")
    validate_device(devices[0])
    return devices[0]


class WindowsHid:
    def __init__(self, info: DeviceInfo):
        validate_device(info)
        self.info = info
        self.api = Api()
        self.handle = None
        self.mutex = None
        self.owns_mutex = False

    def __enter__(self):
        try:
            name = "Local\\AsusDuoLayout-" + hashlib.sha256(self.info.path.casefold().encode()).hexdigest()
            self.mutex = self.api.kernel.CreateMutexW(None, False, name)
            if not self.mutex:
                raise C.WinError(C.get_last_error())
            result = self.api.kernel.WaitForSingleObject(self.mutex, 0)
            if result not in (0, 0x80):  # acquired, or recovered abandoned mutex
                raise LayoutError("Another layout-runner process is using this keyboard.")
            self.owns_mutex = True
            # Re-read the exact selected interface immediately before opening
            # report I/O, rather than trusting a stale enumeration result.
            fresh = self.api.describe(self.info.path)
            if fresh is None:
                raise LayoutError("Keyboard disconnected before opening.")
            validate_device(fresh)
            if fresh != self.info:
                raise LayoutError("Device identity changed during opening. Refusing to continue.")
            self.handle = self.api.kernel.CreateFileW(self.info.path, 0xC0000000, 3, None, 3, 0x40000000, None)
            if self.handle == INVALID_HANDLE:
                self.handle = None
                raise C.WinError(C.get_last_error())
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.handle is not None:
            self.api.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.mutex is not None:
            if self.owns_mutex:
                self.api.kernel.ReleaseMutex(self.mutex)
            self.api.kernel.CloseHandle(self.mutex)
            self.mutex = None
            self.owns_mutex = False

    def _transfer(self, function_name, buffer, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Keyboard report timed out.")
        overlapped = Overlapped()
        overlapped.hEvent = self.api.kernel.CreateEventW(None, True, False, None)
        if not overlapped.hEvent:
            raise C.WinError(C.get_last_error())
        pending = False
        try:
            function = getattr(self.api.kernel, function_name)
            # A Python interrupt can be raised as a ctypes call returns, after
            # Windows already queued the I/O. Mark it before making that call.
            pending = True
            if not function(self.handle, buffer, C.sizeof(buffer), None, C.byref(overlapped)):
                if C.get_last_error() != 997:  # ERROR_IO_PENDING
                    pending = False
                    raise C.WinError(C.get_last_error())
            wait = self.api.kernel.WaitForSingleObject(overlapped.hEvent, max(1, int(remaining * 1000)))
            if wait != 0:
                if wait == 258:
                    raise TimeoutError("Keyboard report timed out; its final state may be unknown.")
                raise C.WinError(C.get_last_error())
            count = W.DWORD()
            if not self.api.kernel.GetOverlappedResult(self.handle, C.byref(overlapped), C.byref(count), False):
                raise C.WinError(C.get_last_error())
            pending = False
            return count.value
        finally:
            if pending:
                # Keep the OVERLAPPED structure and buffer alive until Windows
                # acknowledges cancellation, even when interrupted with Ctrl+C.
                self.api.kernel.CancelIoEx(self.handle, C.byref(overlapped))
                count = W.DWORD()
                self.api.kernel.GetOverlappedResult(self.handle, C.byref(overlapped), C.byref(count), True)
            self.api.kernel.CloseHandle(overlapped.hEvent)

    def exchange(self, command: bytes, timeout=3.0) -> bytes:
        if self.handle is None:
            raise LayoutError("HID transport is closed.")
        if not 2 <= len(command) <= 63:
            raise LayoutError("Invalid command length.")
        report = bytes([0x20]) + command + bytes(63 - len(command))
        outgoing = C.create_string_buffer(report, 64)
        deadline = time.monotonic() + timeout
        if self._transfer("WriteFile", outgoing, deadline) != 64:
            raise LayoutError("Incomplete HID output report; no automatic retry performed.")
        while True:
            incoming = C.create_string_buffer(64)
            count = self._transfer("ReadFile", incoming, deadline)
            if count < 7:
                raise LayoutError("Truncated HID input report.")
            raw = incoming.raw[:count]
            if raw[0] == 0x21 and raw[1:3] == command[:2]:
                return raw[1:]
