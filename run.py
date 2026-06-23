"""
CloakBrowser Manager — Windows launcher.

Double-click run.bat to start, or run:
    python run.py

This script:
1. Installs Python dependencies (first run)
2. Builds the React frontend (first run or when changed)
3. Starts the FastAPI backend on http://localhost:8080
"""

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def step(description: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {description}")
    print("=" * 60)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check)


def main() -> None:
    os.chdir(ROOT)

    print(r"""
   ____ _             _     ____                                _
  / ___| | ___   __ _| | __| __ ) _ __ _____  _____ _ __ ___   __| | ___ _ __
 | |   | |/ _ \ / _` | |/ /|  _ \| '__/ _ \ \/ / _ \ '__/ _ \ / _` |/ _ \ '__|
 | |___| | (_) | (_| |   < | |_) | | | (_) >  <  __/ | | (_) | (_| |  __/ |
  \____|_|\___/ \__,_|_|\_\|____/|_|  \___/_/\_\___|_|  \___/ \__,_|\___|_|

                     Browser Profile Manager
    """)

    # --- 1. Install Python dependencies ---
    step("Installing Python dependencies")
    run([sys.executable, "-m", "pip", "install", "-r", "backend/requirements.txt"], cwd=ROOT)

    # --- 2. Build frontend ---
    step("Building React frontend")
    frontend_dir = ROOT / "frontend"
    dist_dir = frontend_dir / "dist"

    if not (frontend_dir / "node_modules").exists():
        print("  Installing npm packages...")
        run(["npm", "install"], cwd=frontend_dir)

    run(["npm", "run", "build"], cwd=frontend_dir)

    # --- 3. Start backend ---
    step("Starting CloakBrowser Manager")
    print(f"\n  Opening http://localhost:8080 in your browser...")
    print("  Press Ctrl+C to stop.\n")

    # Open browser after a short delay
    webbrowser.open("http://localhost:8080")

    run(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8080"],
        cwd=ROOT,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nShutting down...")
    except subprocess.CalledProcessError as e:
        print(f"\nError: Command failed with exit code {e.returncode}")
        print("Make sure you have Node.js and Python installed.")
        sys.exit(1)
