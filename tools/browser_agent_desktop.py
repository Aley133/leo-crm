from __future__ import annotations

import asyncio
import base64
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from tkinter import Tk, messagebox, simpledialog
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.browser_agent import main as run_browser_agent

API_URL = "https://leo-crm-api.onrender.com"
CDP_ENDPOINT = "http://127.0.0.1:9222"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LEO-CRM" / "browser-agent"
TOKEN_FILE = APP_DIR / "agent-token.dat"
LOG_FILE = APP_DIR / "agent.log"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    target = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "LEO Browser Agent", None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


def _unprotect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    target = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


def _load_saved_token() -> str | None:
    if not TOKEN_FILE.is_file():
        return None
    try:
        encrypted = base64.b64decode(TOKEN_FILE.read_bytes(), validate=True)
        token = _unprotect(encrypted).decode("utf-8").strip()
        return token or None
    except Exception:
        TOKEN_FILE.unlink(missing_ok=True)
        return None


def _save_token(token: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_bytes(base64.b64encode(_protect(token.encode("utf-8"))))


def _ask_token() -> str:
    saved = _load_saved_token()
    if saved:
        return saved
    root = Tk()
    root.withdraw()
    token = simpledialog.askstring(
        "LEO Browser Agent",
        "Вставьте SERVICE_API_TOKEN из LEO CRM. Он будет зашифрован средствами Windows и сохранён только для этого пользователя:",
        show="*",
        parent=root,
    )
    root.destroy()
    if not token or not token.strip():
        raise RuntimeError("SERVICE_API_TOKEN не введён")
    value = token.strip()
    _save_token(value)
    return value


def _verify_crm(token: str) -> None:
    request = Request(
        f"{API_URL}/api/browser-agent/agents",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f"CRM вернула HTTP {response.status}")
    except HTTPError as exc:
        if exc.code == 401:
            TOKEN_FILE.unlink(missing_ok=True)
            raise RuntimeError("API-токен не принят CRM. Запустите агент ещё раз и введите актуальный токен.") from exc
        raise RuntimeError(f"CRM вернула HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"CRM недоступна: {exc}") from exc


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
    profile = APP_DIR / "chrome-profile"
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
    messagebox.showerror("LEO Browser Agent", f"{text}\n\nЛог: {LOG_FILE}", parent=root)
    root.destroy()


def _redirect_output() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    stream = LOG_FILE.open("a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    print(f"\n=== LEO Browser Agent start {time.strftime('%Y-%m-%d %H:%M:%S')} ===")


def main() -> int:
    try:
        _redirect_output()
        token = _ask_token()
        _verify_crm(token)
        _start_browser()
        os.environ["CRM_API_URL"] = API_URL
        os.environ["CRM_SERVICE_TOKEN"] = token
        os.environ["BROWSER_AGENT_ID"] = f"leo-windows-{os.environ.get('COMPUTERNAME', 'pc')}"
        os.environ["CHROME_CDP_ENDPOINT"] = CDP_ENDPOINT
        os.environ["BROWSER_AGENT_POLL_SECONDS"] = "3"
        os.environ["BROWSER_AGENT_CONCURRENCY"] = "1"
        os.environ["BROWSER_AGENT_DISPATCH_LIMIT"] = "100"
        os.environ["BROWSER_AGENT_VERSION"] = "0.1.1"
        return asyncio.run(run_browser_agent())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Fatal error: {exc!r}")
        _show_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
