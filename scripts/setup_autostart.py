"""
scripts/setup_autostart.py
──────────────────────────
Installs / uninstalls silent auto-start for JARVIS Wake Word Daemon on Windows boot.

Usage:
  python scripts/setup_autostart.py --install    (default)
  python scripts/setup_autostart.py --status
  python scripts/setup_autostart.py --remove
"""

import argparse
import os
import sys
from pathlib import Path


def get_paths():
    base_dir = Path(__file__).resolve().parent.parent
    appdata = os.environ.get("APPDATA")
    if not appdata:
        appdata = str(Path.home() / "AppData" / "Roaming")

    startup_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    target_vbs = startup_dir / "JarvisWakeDaemon.vbs"
    return base_dir, startup_dir, target_vbs


def install_autostart():
    base_dir, startup_dir, target_vbs = get_paths()
    startup_dir.mkdir(parents=True, exist_ok=True)

    wake_script = base_dir / "core" / "wake_word.py"
    python_exe = sys.executable

    # Generate silent VBScript launcher with WindowStyle=0 (hidden)
    vbs_content = f'''\' JARVIS Background Wake Word Daemon Auto-Start
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "{base_dir}"
WshShell.Run """{python_exe}"" ""{wake_script}""", 0, False
'''

    target_vbs.write_text(vbs_content, encoding="utf-8")
    print(f"[OK] Silent auto-start installed successfully!")
    print(f"Location: {target_vbs}")
    print(f"Target:   {python_exe} -> {wake_script}")
    print("\nJARVIS will now listen in the background silently whenever Windows boots.")
    print("Say 'Jarvis wake up' or 'Hey Jarvis' at any time to launch the assistant.")


def remove_autostart():
    _, _, target_vbs = get_paths()
    if target_vbs.exists():
        target_vbs.unlink()
        print(f"[OK] Silent auto-start removed ({target_vbs}).")
    else:
        print("[i] Auto-start script was not found in Startup folder.")


def check_status():
    _, _, target_vbs = get_paths()
    if target_vbs.exists():
        print(f"[ACTIVE] Auto-start is INSTALLED at:\n{target_vbs}")
    else:
        print("[INACTIVE] Auto-start is not currently installed.")


def main():
    parser = argparse.ArgumentParser(description="Manage JARVIS Windows Boot Auto-Start")
    parser.add_argument("--install", action="store_true", default=True, help="Install auto-start on boot")
    parser.add_argument("--remove", action="store_true", help="Remove auto-start from boot")
    parser.add_argument("--status", action="store_true", help="Check auto-start status")

    args = parser.parse_args()

    if args.remove:
        remove_autostart()
    elif args.status:
        check_status()
    else:
        install_autostart()


if __name__ == "__main__":
    main()
