from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tools.kaspi_fast_dumping_session import (
    DEFAULT_USER_AGENT,
    MC_OAUTH_ENTRY_URL,
    KaspiMerchantSession,
)


SENSITIVE_QUERY_KEYS = {
    "code",
    "state",
    "nonce",
    "token",
    "access_token",
    "id_token",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return _env(name, fallback).lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


def browser_profile_dir(workspace_id: int) -> Path:
    """Return the durable, workspace-isolated trusted-browser profile."""

    configured = _env("KASPI_FAST_DUMPING_BROWSER_PROFILE_DIR")
    if configured:
        configured = configured.replace("{workspace_id}", str(workspace_id))
        return Path(os.path.expandvars(configured)).expanduser().resolve()
    root = Path(os.getenv("APPDATA") or Path.home()) / "LEO CRM"
    return (root / "kaspi_trusted_browser" / f"workspace-{workspace_id}").resolve()


def _diagnostics_dir(workspace_id: int) -> Path:
    root = Path(os.getenv("APPDATA") or Path.home()) / "LEO CRM"
    return root / "diagnostics" / f"workspace-{workspace_id}"


def _playwright_import():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "В этой версии Fast Agent отсутствует модуль Chromium. "
            "Скачайте свежий LEO Kaspi Fast Dumping Agent."
        ) from exc
    return sync_playwright


def _installed_chromium_candidates() -> list[Path]:
    """Find a local Chromium-family browser if Playwright's copy is absent."""

    candidates: list[Path] = []
    explicit = _env("KASPI_FAST_DUMPING_BROWSER_EXECUTABLE")
    if explicit:
        candidates.append(Path(os.path.expandvars(explicit)).expanduser())

    local = os.getenv("LOCALAPPDATA")
    program_files = os.getenv("PROGRAMFILES")
    program_files_x86 = os.getenv("PROGRAMFILES(X86)")
    if local:
        candidates.extend(
            [
                Path(local) / "Google/Chrome/Application/chrome.exe",
                Path(local) / "Microsoft/Edge/Application/msedge.exe",
                Path(local) / "Chromium/Application/chrome.exe",
            ]
        )
    for root in (program_files, program_files_x86):
        if root:
            candidates.extend(
                [
                    Path(root) / "Google/Chrome/Application/chrome.exe",
                    Path(root) / "Microsoft/Edge/Application/msedge.exe",
                    Path(root) / "Chromium/Application/chrome.exe",
                ]
            )

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        marker = str(candidate).casefold()
        if marker in seen or not candidate.is_file():
            continue
        seen.add(marker)
        result.append(candidate)
    return result


def _launch_persistent_context(
    chromium: Any,
    *,
    profile_dir: Path,
    headless: bool,
    user_agent: str,
) -> Any:
    common = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "user_agent": user_agent,
        "locale": "ru-RU",
        "viewport": {"width": 1280, "height": 900},
    }
    attempts: list[tuple[str, str | None]] = []

    try:
        bundled = Path(chromium.executable_path)
    except (AttributeError, TypeError):
        bundled = Path()
    launch_targets: list[Path | None] = []
    if bundled.is_file():
        launch_targets.append(None)
    launch_targets.extend(_installed_chromium_candidates())
    if not launch_targets:
        launch_targets.append(None)

    for executable in launch_targets:
        try:
            kwargs = dict(common)
            if executable is not None:
                kwargs["executable_path"] = str(executable)
            return chromium.launch_persistent_context(**kwargs)
        except Exception as exc:  # pragma: no cover - depends on local browsers
            label = "Playwright Chromium" if executable is None else executable.name
            attempts.append((label, type(exc).__name__))

    tried = ", ".join(f"{label} ({error})" for label, error in attempts)
    raise RuntimeError(
        "Не удалось открыть постоянный Chromium-профиль Fast Agent. "
        "Закройте оставшееся окно Kaspi/Chromium и повторите. "
        f"Проверенные браузеры: {tried or 'не найдены'}."
    )


def _visible_locator(page: Any, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        for index in range(min(count, 30)):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item
            except Exception:
                pass
    return None


def _all_visible_inputs(page: Any) -> list[Any]:
    result: list[Any] = []
    try:
        inputs = page.locator("input")
        for index in range(min(inputs.count(), 40)):
            item = inputs.nth(index)
            try:
                if item.is_visible() and item.is_enabled():
                    result.append(item)
            except Exception:
                pass
    except Exception:
        pass
    return result


def _input_kind(item: Any) -> tuple[str, str, str, str, str]:
    def attribute(name: str) -> str:
        try:
            return (item.get_attribute(name) or "").strip().lower()
        except Exception:
            return ""

    return (
        attribute("type"),
        attribute("name"),
        attribute("id"),
        attribute("autocomplete"),
        attribute("placeholder"),
    )


def _pick_username_input(page: Any):
    found = _visible_locator(
        page,
        [
            'input[autocomplete="username"]',
            'input[type="email"]',
            'input[name="_u"]',
            'input[name*="email" i]',
            'input[id*="email" i]',
            'input[placeholder*="почт" i]',
            'input[placeholder*="email" i]',
            'input[placeholder*="логин" i]',
        ],
    )
    if found is not None:
        return found
    for item in _all_visible_inputs(page):
        kind, name, identifier, _autocomplete, placeholder = _input_kind(item)
        haystack = name + identifier + placeholder
        if kind in {"text", "email", "tel", ""} and not re.search(
            r"code|otp|код", haystack, re.I
        ):
            return item
    return None


def _pick_password_input(page: Any):
    return _visible_locator(
        page,
        [
            'input[autocomplete="current-password"]',
            'input[type="password"]',
            'input[name="_p"]',
            'input[name*="pass" i]',
            'input[id*="pass" i]',
            'input[placeholder*="парол" i]',
            'input[placeholder*="password" i]',
        ],
    )


def _fill_verified(item: Any, value: str, *, label: str) -> None:
    item.scroll_into_view_if_needed()
    item.click(force=True, timeout=3000)
    item.fill(value, timeout=5000)
    try:
        current = item.input_value(timeout=2000)
    except Exception:
        current = ""
    if current != value:
        item.press("Control+A")
        item.type(value, delay=20)
        try:
            current = item.input_value(timeout=2000)
        except Exception:
            current = ""
    if current != value:
        raise RuntimeError(
            f"Поле Kaspi ({label}) найдено, но введённое значение не сохранилось"
        )


def _visible_otp_inputs(page: Any) -> list[Any]:
    preferred = [
        'input[autocomplete="one-time-code"]',
        'input[name*="otp" i]',
        'input[id*="otp" i]',
        'input[name*="code" i]',
        'input[id*="code" i]',
        'input[placeholder*="код" i]',
        'input[inputmode="numeric"]',
    ]
    found: list[Any] = []
    markers: set[str] = set()
    for selector in preferred:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        for index in range(min(count, 20)):
            item = locator.nth(index)
            try:
                if not (item.is_visible() and item.is_enabled()):
                    continue
                marker = item.evaluate("el => el.outerHTML.slice(0,300)")
                if marker not in markers:
                    markers.add(marker)
                    found.append(item)
            except Exception:
                pass
    if found:
        return found
    locator = page.locator('input[maxlength="1"]')
    try:
        segmented = [
            locator.nth(index)
            for index in range(min(locator.count(), 12))
            if locator.nth(index).is_visible() and locator.nth(index).is_enabled()
        ]
    except Exception:
        segmented = []
    return segmented if len(segmented) >= 4 else []


def _page_signature(page: Any) -> tuple[str, int, int]:
    try:
        return page.url, page.locator("input").count(), page.locator("button").count()
    except Exception:
        return page.url, 0, 0


def _robust_submit(
    page: Any,
    labels: list[str],
    *,
    focus: Any | None = None,
    required: bool = True,
) -> bool:
    before = _page_signature(page)
    if focus is not None:
        try:
            focus.press("Enter", timeout=2500)
            page.wait_for_timeout(700)
            if _page_signature(page) != before:
                return True
        except Exception:
            pass

    selectors = [f'button:has-text("{label}")' for label in labels]
    selectors.extend(['button[type="submit"]', 'input[type="submit"]'])
    button = _visible_locator(page, selectors)
    if button is None:
        form = _visible_locator(page, ["form"])
        if form is not None:
            try:
                form.evaluate("f => f.requestSubmit ? f.requestSubmit() : f.submit()")
                return True
            except Exception:
                pass
        if required:
            raise RuntimeError(
                "Не найдена кнопка продолжения входа Kaspi "
                f"({', '.join(labels)})"
            )
        return False

    for mode in ("normal", "force", "dom"):
        try:
            if mode == "normal":
                button.click(timeout=2500)
            elif mode == "force":
                button.click(force=True, timeout=2500)
            else:
                button.evaluate("el => el.click()")
            return True
        except Exception:
            continue
    if required:
        raise RuntimeError("Кнопка продолжения входа Kaspi найдена, но не нажимается")
    return False


def _ensure_checked(checkbox: Any) -> bool:
    try:
        if not checkbox.is_visible():
            return False
        if not checkbox.is_checked():
            checkbox.check(force=True)
        return checkbox.is_checked()
    except Exception:
        return False


def _remember_user(page: Any) -> bool:
    """Enable Kaspi's remember-user/device checkbox without guessing its value."""

    direct = _visible_locator(
        page,
        [
            'input[type="checkbox"][name*="remember" i]',
            'input[type="checkbox"][id*="remember" i]',
            'input[type="checkbox"][name*="trust" i]',
            'input[type="checkbox"][id*="trust" i]',
            '[role="checkbox"][aria-label*="запом" i]',
            '[role="checkbox"][aria-label*="remember" i]',
        ],
    )
    if direct is not None:
        try:
            role = (direct.get_attribute("role") or "").lower()
        except Exception:
            role = ""
        if role == "checkbox":
            try:
                if direct.get_attribute("aria-checked") != "true":
                    direct.click(force=True)
                return direct.get_attribute("aria-checked") == "true"
            except Exception:
                pass
        elif _ensure_checked(direct):
            return True

    text_re = re.compile(
        r"(запомн|сохран.{0,12}(вход|польз|устрой)|довер.{0,12}устрой|"
        r"remember|trust.{0,12}device|keep.{0,12}(signed|login))",
        re.I,
    )
    try:
        labels = page.locator("label")
        for index in range(min(labels.count(), 30)):
            label = labels.nth(index)
            try:
                if not label.is_visible():
                    continue
                text = re.sub(r"\s+", " ", label.inner_text()).strip()
                if not text_re.search(text):
                    continue
                checkbox = label.locator('input[type="checkbox"]')
                if checkbox.count() and _ensure_checked(checkbox.first):
                    return True
                target_id = label.get_attribute("for")
                if target_id:
                    escaped = target_id.replace('"', '\\"')
                    linked = page.locator(
                        f'input[type="checkbox"][id="{escaped}"]'
                    )
                    if linked.count() and _ensure_checked(linked.first):
                        return True
                label.click(force=True)
                return True
            except Exception:
                pass
    except Exception:
        pass

    try:
        boxes = page.locator('input[type="checkbox"]')
        if boxes.count() == 1 and _ensure_checked(boxes.first):
            return True
    except Exception:
        pass
    return False


def _normalise_manual_otp(value: str, digits: int) -> str:
    code = re.sub(r"\D+", "", value or "")
    if len(code) != digits:
        raise ValueError(f"Ожидается код из {digits} цифр, получено: {len(code)}")
    return code


def _prompt_manual_otp(*, recipient: str, digits: int) -> str | None:
    """Read an OTP locally. The code is neither logged nor persisted."""

    prompt = (
        f"Kaspi отправил код на {recipient}.\n\n"
        f"Введите {digits}-значный код из письма.\n"
        "Fast Agent сам вставит его в Kaspi; код нигде не сохранится.\n\n"
        "Cancel — ввести код прямо в открытом окне Kaspi."
    )
    try:
        import tkinter as tk
        from tkinter import simpledialog

        from tkinter import messagebox

        root = tk.Tk()
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            while True:
                value = simpledialog.askstring(
                    "LEO Fast Agent — код Kaspi",
                    prompt,
                    parent=root,
                )
                if value is None:
                    return None
                try:
                    return _normalise_manual_otp(value, digits)
                except ValueError:
                    messagebox.showerror(
                        "LEO Fast Agent — код Kaspi",
                        f"Введите ровно {digits} цифр из письма.",
                        parent=root,
                    )
        finally:
            root.destroy()
    except Exception:
        while True:
            try:
                value = input(
                    f"Введите {digits}-значный код Kaspi из письма "
                    "(Enter = ввести в браузере): "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not value:
                return None
            try:
                return _normalise_manual_otp(value, digits)
            except ValueError:
                print(f"Нужны ровно {digits} цифр. Попробуйте ещё раз.", flush=True)


def _fill_otp(inputs: list[Any], code: str) -> None:
    if not inputs:
        raise RuntimeError("Поле для кода Kaspi не найдено")
    if len(inputs) == 1:
        _fill_verified(inputs[0], code, label="одноразовый код")
        return
    if len(inputs) < len(code):
        raise RuntimeError(
            f"Kaspi показал {len(inputs)} полей, а в коде {len(code)} цифр"
        )
    for item, digit in zip(inputs, code, strict=False):
        _fill_verified(item, digit, label="цифра одноразового кода")


def _mc_sid_from_context(context: Any) -> str | None:
    for cookie in context.cookies():
        if cookie.get("name") == "mc-sid" and cookie.get("value"):
            return str(cookie["value"])
    return None


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = [
            (key, "***" if key.lower() in SENSITIVE_QUERY_KEYS else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    except Exception:
        return "<unprintable-url>"


def _safe_diagnostics(
    page: Any,
    context: Any,
    *,
    workspace_id: int,
    stage: str,
    error_type: str | None = None,
) -> str:
    directory = _diagnostics_dir(workspace_id)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = directory / f"kaspi_auth_{stamp}_{stage}.json"
    inputs: list[dict[str, Any]] = []
    try:
        locator = page.locator("input")
        for index in range(min(locator.count(), 40)):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                inputs.append(
                    {
                        "type": item.get_attribute("type"),
                        "name": item.get_attribute("name"),
                        "id": item.get_attribute("id"),
                        "autocomplete": item.get_attribute("autocomplete"),
                        "placeholder": item.get_attribute("placeholder"),
                        "maxlength": item.get_attribute("maxlength"),
                        "inputmode": item.get_attribute("inputmode"),
                    }
                )
            except Exception:
                pass
    except Exception:
        pass
    buttons: list[str] = []
    try:
        locator = page.locator("button")
        for index in range(min(locator.count(), 30)):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    text = re.sub(r"\s+", " ", item.inner_text()).strip()
                    if text:
                        buttons.append(text[:120])
            except Exception:
                pass
    except Exception:
        pass
    try:
        title = page.title()[:200]
    except Exception:
        title = None
    try:
        cookie_names = sorted(
            {str(cookie.get("name")) for cookie in context.cookies()}
        )
    except Exception:
        cookie_names = []
    payload = {
        "workspace_id": workspace_id,
        "stage": stage,
        "error_type": error_type,
        "url": _redact_url(getattr(page, "url", "")),
        "title": title,
        "input_descriptors": inputs,
        "button_labels": buttons,
        "cookie_names": cookie_names,
        "profile_dir": str(browser_profile_dir(workspace_id)),
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)
    except OSError:
        return "недоступна (не удалось записать файл)"


def _wait_for_password_or_otp(
    page: Any,
    *,
    deadline: float,
) -> tuple[Any | None, list[Any]]:
    while time.monotonic() < deadline:
        password = _pick_password_input(page)
        otp = _visible_otp_inputs(page)
        if password is not None or otp:
            return password, otp
        page.wait_for_timeout(350)
    return None, []


def _wait_for_sid(context: Any, page: Any, *, deadline: float) -> str | None:
    while time.monotonic() < deadline:
        sid = _mc_sid_from_context(context)
        if sid:
            return sid
        page.wait_for_timeout(350)
    return None


def refresh_mc_sid_via_browser(
    *,
    workspace_id: int,
    email: str,
    password: str,
    timeout: float = 240.0,
    user_agent: str = DEFAULT_USER_AGENT,
    log: Callable[[str], None] | None = None,
) -> str:
    """Recover mc-sid through a durable, locally trusted Chromium profile."""

    sync_playwright = _playwright_import()
    auth_entry = _env("KASPI_MC_OAUTH_ENTRY_URL", MC_OAUTH_ENTRY_URL)
    try:
        otp_digits = int(_env("KASPI_OTP_DIGITS", "6") or "6")
    except ValueError as exc:
        raise RuntimeError("KASPI_OTP_DIGITS должен быть целым числом") from exc
    if not 4 <= otp_digits <= 12:
        raise RuntimeError("KASPI_OTP_DIGITS должен быть от 4 до 12")
    otp_mode = (_env("KASPI_OTP_ENTRY_MODE", "dialog") or "dialog").lower()
    if otp_mode not in {"dialog", "browser"}:
        raise RuntimeError("KASPI_OTP_ENTRY_MODE: допустимы dialog или browser")
    headless = _env_bool("KASPI_2FA_HEADLESS", False)
    if headless and otp_mode == "browser":
        raise RuntimeError("Режим browser требует KASPI_2FA_HEADLESS=false")

    timeout = max(60.0, timeout)
    deadline = time.monotonic() + timeout
    profile_dir = browser_profile_dir(workspace_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    emit = log or (lambda _message: None)

    with sync_playwright() as playwright:
        context = _launch_persistent_context(
            playwright.chromium,
            profile_dir=profile_dir,
            headless=headless,
            user_agent=user_agent,
        )
        page = context.pages[0] if context.pages else context.new_page()
        stage = "open"
        try:
            # The Merchant session itself must be renewed, while IdP trust cookies
            # and localStorage remain in the persistent workspace profile.
            try:
                context.clear_cookies(name="mc-sid")
            except Exception:
                pass

            emit("Сессия Kaspi истекла: открыт постоянный Chromium для восстановления mc-sid.")
            page.goto(
                auth_entry,
                wait_until="domcontentloaded",
                timeout=min(60_000, int(timeout * 1000)),
            )
            page.wait_for_timeout(800)
            sid = _mc_sid_from_context(context)
            if sid:
                return sid

            stage = "credentials_username"
            username = None
            username_deadline = min(deadline, time.monotonic() + 25)
            while time.monotonic() < username_deadline:
                username = _pick_username_input(page)
                if (
                    username is not None
                    or _pick_password_input(page) is not None
                    or _visible_otp_inputs(page)
                ):
                    break
                sid = _mc_sid_from_context(context)
                if sid:
                    return sid
                page.wait_for_timeout(400)

            if username is not None:
                _fill_verified(username, email, label="логин")

            password_input = _pick_password_input(page)
            if password_input is None and not _visible_otp_inputs(page):
                if username is not None:
                    _robust_submit(
                        page,
                        ["Продолжить", "Далее", "Войти", "Continue", "Next"],
                        focus=username,
                    )
                else:
                    _robust_submit(
                        page,
                        ["Продолжить", "Далее", "Войти", "Continue", "Next"],
                        required=False,
                    )
                password_input, early_otp = _wait_for_password_or_otp(
                    page,
                    deadline=min(deadline, time.monotonic() + 25),
                )
                if early_otp:
                    password_input = None

            if password_input is not None:
                stage = "credentials_password"
                _fill_verified(password_input, password, label="пароль")
                _robust_submit(
                    page,
                    ["Войти", "Продолжить", "Далее", "Login", "Continue"],
                    focus=password_input,
                )

            stage = "wait_challenge"
            challenge_deadline = min(deadline, time.monotonic() + 35)
            inputs: list[Any] = []
            while time.monotonic() < challenge_deadline:
                page.wait_for_timeout(350)
                sid = _mc_sid_from_context(context)
                if sid:
                    return sid
                inputs = _visible_otp_inputs(page)
                if inputs:
                    break

            if not inputs:
                sid = _wait_for_sid(
                    context,
                    page,
                    deadline=min(deadline, time.monotonic() + 12),
                )
                if sid:
                    return sid
                diagnostics = _safe_diagnostics(
                    page,
                    context,
                    workspace_id=workspace_id,
                    stage=stage,
                    error_type="challenge_not_found",
                )
                raise RuntimeError(
                    "Kaspi не завершил вход и не показал узнаваемое поле кода. "
                    f"Безопасная диагностика: {diagnostics}"
                )

            stage = "remember_user"
            remembered = _remember_user(page)
            if remembered:
                emit("Kaspi запросил код: включено «Запомнить вход с этого устройства».")
            else:
                emit(
                    "Kaspi запросил код. Поле найдено, но галочка запоминания не распознана; "
                    "вход будет продолжен без остановки."
                )

            code: str | None = None
            if otp_mode == "dialog":
                stage = "manual_otp_dialog"
                code = _prompt_manual_otp(recipient=email, digits=otp_digits)

            if code:
                stage = "submit_otp"
                _fill_otp(inputs, code)
                page.wait_for_timeout(300)
                if not _mc_sid_from_context(context):
                    _robust_submit(
                        page,
                        ["Продолжить", "Подтвердить", "Войти", "Confirm", "Submit"],
                        focus=inputs[-1] if inputs else None,
                        required=False,
                    )
            else:
                stage = "manual_otp_browser"
                emit(
                    "Введите код из письма прямо в открытом окне Kaspi и нажмите «Продолжить»."
                )

            stage = "wait_mc_sid"
            sid = _wait_for_sid(context, page, deadline=deadline)
            if sid:
                page.wait_for_timeout(1200)
                emit("Новый mc-sid получен; Fast Dumping продолжает очередь автоматически.")
                return sid

            diagnostics = _safe_diagnostics(
                page,
                context,
                workspace_id=workspace_id,
                stage=stage,
                error_type="mc_sid_not_obtained",
            )
            raise RuntimeError(
                "Kaspi не выдал mc-sid до окончания ожидания. "
                f"Безопасная диагностика: {diagnostics}"
            )
        except Exception as exc:
            if "Безопасная диагностика:" not in str(exc):
                diagnostics = _safe_diagnostics(
                    page,
                    context,
                    workspace_id=workspace_id,
                    stage=stage,
                    error_type=type(exc).__name__,
                )
                raise RuntimeError(
                    "Не удалось восстановить сессию Kaspi. "
                    f"Безопасная диагностика: {diagnostics}"
                ) from exc
            raise
        finally:
            try:
                context.close()
            except Exception:
                pass


class TrustedBrowserKaspiMerchantSession(KaspiMerchantSession):
    """Kaspi session whose renewal path supports the interactive email OTP."""

    def __init__(
        self,
        *,
        workspace_id: int,
        log: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.workspace_id = workspace_id
        self.auth_log = log

    def refresh_sid(self) -> str:
        mode = _env("KASPI_FAST_DUMPING_AUTH_MODE", "browser_trusted").lower()
        if mode in {"legacy_http", "http"}:
            return super().refresh_sid()
        if mode not in {"browser_trusted", "trusted_browser", "auto"}:
            raise RuntimeError(
                "KASPI_FAST_DUMPING_AUTH_MODE: допустимы browser_trusted или legacy_http"
            )
        return refresh_mc_sid_via_browser(
            workspace_id=self.workspace_id,
            email=self.email,
            password=self.password,
            timeout=max(
                60.0,
                _env_float("KASPI_2FA_TOTAL_TIMEOUT_SECONDS", 240.0),
            ),
            user_agent=self.user_agent,
            log=self.auth_log,
        )
