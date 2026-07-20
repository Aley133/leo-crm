from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from tkinter import Tk, messagebox, simpledialog
from urllib.error import URLError
from urllib.request import urlopen

from tools.browser_agent import main as run_browser_agent

API_URL = "https://leo-crm-api.onrender.com"
CDP_ENDPOINT = "http://127.0.0.1:9222"


def _ask_token() -> str:
    root = Tk()
    root.withdraw()
    token = simpledialog.askstring(
        "LEO Browser Agent",
        "Вставьте SERVICE_API_TOKEN из LEO CRM:",
        show="*",
        parent=root,
    )
    root.destroy()
    if not token or not token.strip():
        raise RuntimeError("SERVICE_API_TOKEN не введён")
    return token.strip()


def _find_browser() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Google Chrome или Microsoft Edge не найден")


def _cdp_ready() -> bool:
    try:
        with urlopen(f"{CDP_ENDPOINT}/json/version", timeout=2):
            return True
    except (URLError, TimeoutError, OSError):
        return False


def _start_browser() -> None:
    if _cdp_ready():
        return
    browser = _find_browser()
    profile = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LEO-CRM/browser-agent/chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(browser),
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.ozon.ru/",
        ],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    for _ in range(30):
        if _cdp_ready():
            return
        time.sleep(1)
    raise RuntimeError("Браузер запущен, но порт 9222 не отвечает")


def _show_error(text: str) -> None:
    root = Tk()
    root.withdraw()
    messagebox.showerror("LEO Browser Agent", text, parent=root)
    root.destroy()


def main() -> int:
    try:
        token = _ask_token()
        _start_browser()
        os.environ["CRM_API_URL"] = API_URL
        os.environ["CRM_SERVICE_TOKEN"] = token
        os.environ["BROWSER_AGENT_ID"] = f"leo-windows-{os.environ.get('COMPUTERNAME', 'pc')}"
        os.environ["CHROME_CDP_ENDPOINT"] = CDP_ENDPOINT
        os.environ["BROWSER_AGENT_POLL_SECONDS"] = "3"
        os.environ["BROWSER_AGENT_CONCURRENCY"] = "1"
        os.environ["BROWSER_AGENT_DISPATCH_LIMIT"] = "100"
        return asyncio.run(run_browser_agent())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _show_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
