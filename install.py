#!/usr/bin/env python
"""Installation script to create virtual environment and install package."""

import os
import sys
import subprocess
import platform
import shutil

def run_command(cmd, description):
    """Run a command and handle errors."""
    print(f"\n{'='*60}")
    print(f"[*] {description}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(cmd, shell=True, check=True)
        if result.returncode == 0:
            print(f"[OK] {description} - Success!")
        return result.returncode
    except subprocess.CalledProcessError:
        print(f"[ERR] {description} - Failed!")
        sys.exit(1)

def main():
    print("\n" + "="*60)
    print("[SETUP] AniList-MAL Sync - One-Time Setup")
    print("="*60)
    
    is_windows = platform.system() == "Windows"
    
    # Remove existing venv if it exists
    if os.path.exists(".venv"):
        print("\n[*] Removing existing virtual environment...")
        shutil.rmtree(".venv")
        if not os.path.exists(".venv"):
            print("[OK] Removed existing virtual environment")
        else:
            print("[ERR] Failed to remove existing virtual environment")
            sys.exit(1)
    
    print("\n[1/2] Creating virtual environment...")
    venv_cmd = "py -m venv .venv" if is_windows else "python3 -m venv .venv"
    run_command(venv_cmd, "Create virtual environment")
    
    print("\n[2/2] Installing package...")
    if is_windows:
        pip_cmd = ".venv\\Scripts\\python.exe -m pip install -e ."
    else:
        pip_cmd = "./.venv/bin/python -m pip install -e ."
    run_command(pip_cmd, "Install package and dependencies")
    
    print("\n" + "="*60)
    print("[OK] Setup Complete!")
    print("="*60)
    print("\n[NEXT] What to do now:")
    print("\n1. Create data/config.yaml from config.example.yaml:")
    if is_windows:
        print("   mkdir data")
        print("   copy config.example.yaml data\\config.yaml")
    else:
        print("   mkdir -p data")
        print("   cp config.example.yaml data/config.yaml")
    
    print("\n2. Edit data/config.yaml with both services' client IDs and secrets.")
    print("   Usernames are optional and verify which accounts you intend to sync.")
    
    print("\n3. Activate virtual environment:")
    if is_windows:
        print("   .venv\\Scripts\\activate")
    else:
        print("   source .venv/bin/activate")
    
    print("\n4. Run commands:")
    print("   anilist-mal-sync auth")
    print("   anilist-mal-sync run --once --dry-run")
    print("   anilist-mal-sync run")
    
    print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    main()
