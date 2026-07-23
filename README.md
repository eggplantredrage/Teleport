Teleport is a small cross-platform desktop app for dual-boot systems. Instead of restarting and manually catching the GRUB menu or mashing a boot-device hotkey, Teleport lets you pick your target OS from a simple GUI and reboots straight into it.

It works by setting a one-time next-boot override at the firmware/bootloader level — efibootmgr/grub-reboot on Linux, bcdedit on Windows — so your normal default boot order is untouched after that single reboot. Each detected boot entry shows the physical drive/partition it points to, so you can confirm you're teleporting to the right OS before committing.

Linux edition: auto-detects UEFI vs. legacy BIOS/GRUB, lists all boot entries with drive info via lsblk, reboots via pkexec.
Windows edition: reads UEFI firmware entries via bcdedit, elevates via UAC automatically, restarts via shutdown /r.

Built with PyQt6. No internet connection, telemetry, or background services — it does one thing, then gets out of the way.
