"""
Launches dashboard.py and opens it in Chrome specifically.
=============================================================

`streamlit run dashboard.py` opens whatever the OS considers the default
browser via Python's `webbrowser` module, which isn't always predictable.
This launcher starts Streamlit in headless mode (so Streamlit itself
never tries to open a browser) and then explicitly launches Chrome
pointed at the dashboard once the server is actually responding.

Usage:
    python run_dashboard.py

Ctrl+C stops both the browser-wait and the underlying Streamlit server.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8501
URL = f"http://localhost:{PORT}"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
]


def find_chrome() -> str | None:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return shutil.which("chrome") or shutil.which("google-chrome")


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    script_dir = Path(__file__).parent
    proc = subprocess.Popen([
        sys.executable, "-m", "streamlit", "run", str(script_dir / "dashboard.py"),
        "--server.headless", "true", f"--server.port={PORT}",
    ])

    try:
        if wait_for_server(URL):
            chrome = find_chrome()
            if chrome:
                subprocess.Popen([chrome, URL])
            else:
                print("[warn] Chrome not found in common install locations -- "
                      "opening system default browser instead.")
                webbrowser.open(URL)
        else:
            print(f"[warn] Dashboard didn't respond within timeout -- open {URL} manually.")

        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
