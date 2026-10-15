# ASUS Zenbook Duo keyboard layout - local Windows runner

Local fork of [Hiumee/asus-zenbook-duo-copilot](https://github.com/Hiumee/asus-zenbook-duo-copilot), based on commit `cf992eb2a915da5436c01262169f2e8715ae4281`.

Switches the detachable UX8406CA keyboard between **Legacy (Right Ctrl)** and **Copilot (Win + Shift + F23)** using commands discovered by the upstream author. Independent research, not affiliated with ASUS.

## Run it

Requires Windows and Python 3.11 or newer. There are **no pip dependencies**. Connect the detachable keyboard directly by USB, close the original web tool, and open PowerShell in this folder.

Check the reported revision and current layout (no persistent write):

```powershell
python keyboard_layout.py status
```

Back up the current layout and switch to Right Ctrl:

```powershell
python keyboard_layout.py set legacy
```

Switch back to Copilot:

```powershell
python keyboard_layout.py set copilot
```

Make a backup without changing the layout:

```powershell
python keyboard_layout.py backup
```

Restore the original layout from a particular backup (use the actual filename printed by the script):

```powershell
python keyboard_layout.py restore ".\backups\layout-YYYYMMDDTHHMMSS.ffffffZ-xxxxxxxx.json"
```

Restore also backs up the current state before writing. If the keyboard provides a serial number, it must match the backup. Otherwise the Windows device path must match, so use the same USB connection. Restore refuses to change regional/SKU bits.

## What is checked and saved

- The exact USB vendor/product ID `0B05:1BF2`, vendor interface usage `FFE0:0001`, and 64-byte input/output report sizes must match.
- Windows `HidD_GetAttributes.VersionNumber` must be exactly **`0x0204`**. This is the USB/device revision corresponding to the author's tested firmware 2.04, **not an independently queried internal firmware version**. There is no force/override option. Unsupported revisions receive no configuration commands.
- Device identity/revision is checked again immediately before report I/O is opened. Multiple matching keyboards are refused. A named Windows mutex prevents overlapping operations by copies of this runner in the same Windows session.
- Each change requires a readable, flushed-to-disk JSON backup under **`backups/` next to `keyboard_layout.py`**, regardless of the terminal's current directory. Backup failure prevents the layout write. An already-correct layout causes no persistent write or new backup.
- Backups contain the original layout byte, requested byte, device identity/revision, timestamp and operation outcome. They are **layout-setting backups, not full keyboard configuration or firmware images**. Backups are ignored by Git because they include local device identifiers.
- Only bit 7 of the layout value changes; lower regional/SKU bits are retained. The runner checks the acknowledgement and reads back the saved layout.

This fork is a local Python tool. WebHID cannot provide the checked revision or automatically save backups in the script's folder. Original browser source remains available in Git history and the upstream repository.

## Failure handling and recovery

The runner always attempts to leave configuration mode after an entry attempt, including entry timeouts, invalid responses, subsequent failures and Ctrl+C. It preserves both the original error and any cleanup error. Windows handles and the process mutex are released; pending HID I/O is cancelled before its memory is freed.

After a write timeout, failed verification, failed journal update or failed cleanup, the outcome may be **indeterminate**, even if the keyboard actually changed. The script retains the original backup, reports its path, and never automatically retries a write or writes a rollback. If the final journal update fails, `write_pending` or `verified` may remain in the backup; neither guarantees cleanup completed.

Keep the keyboard connected to stable power during the operation. If a command fails, reconnect the keyboard if necessary and run `status` before deciding to retry or restore. Do not run the upstream website or another keyboard configuration tool at the same time. Abrupt process termination, unplugging, power loss and firmware faults cannot be made safe by Python cleanup.

No replacement firmware image is uploaded. The existing firmware may erase/write flash internally to persist its layout setting. The firmware command's internal implementation is undocumented, and software mocks cannot establish hardware safety or atomicity.

## Validation

```powershell
python -m unittest -v
```

Tests use simulated devices and temporary backup folders. They cover revision rejection before configuration commands, backup failures blocking writes, entry/exit timeouts, interruption cleanup, uncertain write outcomes, read-back verification, regional-bit preservation, identity-checked restoration and HID report framing. They do not access a keyboard.
