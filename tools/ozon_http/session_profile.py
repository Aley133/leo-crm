from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

# Consumer Ozon uses ozon.ru in Russia and ozon.kz in Kazakhstan.
# The lab only permits HTTPS requests to these exact Ozon-owned domain families.
ALLOWED_HOST_SUFFIXES = (".ozon.ru", ".ozon.kz")
ALLOWED_ROOT_HOSTS = {"ozon.ru", "ozon.kz"}
DROP_HEADERS = {
    "content-length",
    "host",
    "connection",
}
SENSITIVE_HEADERS = {"cookie", "authorization", "proxy-authorization", "x-api-key"}


def _normalize_host(host: str | None) -> str:
    return (host or "").split(":", 1)[0].lower().strip().rstrip(".")


def _is_ozon_host(host: str | None) -> bool:
    host = _normalize_host(host)
    return host in ALLOWED_ROOT_HOSTS or any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def _split_header(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    name, value = line.split(":", 1)
    name = name.strip()
    if not name:
        return None
    return name, value.strip()


def _tokenize_curl(text: str) -> list[str]:
    cleaned = str(text or "").strip().lstrip("\ufeff")
    # Chrome Copy as cURL (bash): backslash + newline.
    cleaned = cleaned.replace("\\\r\n", " ").replace("\\\n", " ")
    # Chrome Copy as cURL (cmd) / some clipboard managers: caret + newline.
    cleaned = cleaned.replace("^\r\n", " ").replace("^\n", " ")
    # Be forgiving when a line continuation has trailing spaces after slash/caret.
    cleaned = re.sub(r"\\[ \t]+\r?\n", " ", cleaned)
    cleaned = re.sub(r"\^[ \t]+\r?\n", " ", cleaned)
    try:
        return shlex.split(cleaned, posix=True)
    except ValueError as exc:
        raise ValueError(f"Не удалось разобрать cURL: {exc}") from exc


def _safe_candidate_url(tokens: list[str]) -> str | None:
    """Best-effort URL extraction used by the parser and diagnostics."""
    url: str | None = None
    i = 1
    while i < len(tokens):
        token = tokens[i].strip()
        low = token.lower()
        if low == "--url":
            i += 1
            if i < len(tokens):
                return tokens[i].strip()
        # Accept accidental leading shell continuation characters.
        candidate = token.lstrip("^\\").strip()
        if candidate.lower().startswith(("http://", "https://")):
            url = candidate
            break
        i += 1
    return url


@dataclass(slots=True)
class CurlProfile:
    url: str
    headers: dict[str, str]
    cookie: str | None = None
    method: str = "GET"

    @classmethod
    def parse(cls, curl_text: str) -> "CurlProfile":
        tokens = _tokenize_curl(curl_text)
        if not tokens or tokens[0].lower().rstrip(".exe") != "curl":
            raise ValueError("Вставь полный 'Copy as cURL (bash)' — строка должна начинаться с curl")

        url: str | None = _safe_candidate_url(tokens)
        headers: dict[str, str] = {}
        cookie: str | None = None
        method = "GET"
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token in {"-H", "--header"}:
                i += 1
                if i >= len(tokens):
                    raise ValueError("После -H отсутствует заголовок")
                pair = _split_header(tokens[i])
                if pair:
                    name, value = pair
                    lname = name.lower()
                    if lname == "cookie":
                        cookie = value
                    elif lname not in DROP_HEADERS:
                        headers[name] = value
            elif token in {"-b", "--cookie"}:
                i += 1
                if i >= len(tokens):
                    raise ValueError("После -b отсутствуют cookies")
                cookie = tokens[i]
            elif token in {"-X", "--request"}:
                i += 1
                if i >= len(tokens):
                    raise ValueError("После -X отсутствует HTTP метод")
                method = tokens[i].upper()
            elif token == "--url":
                i += 1
                if i >= len(tokens):
                    raise ValueError("После --url отсутствует URL")
                url = tokens[i].lstrip("^\\").strip()
            elif token in {"--data", "--data-raw", "--data-binary", "--data-urlencode", "-d"}:
                # Search result request should be GET. A pasted POST is rejected explicitly below.
                i += 1
                method = "POST" if method == "GET" else method
            i += 1

        if not url:
            raise ValueError("В cURL не найден URL")
        url = url.strip().strip('"').strip("'")
        parts = urlsplit(url)
        detected_host = _normalize_host(parts.hostname)
        if parts.scheme.lower() != "https":
            raise ValueError(f"Нужен HTTPS URL Ozon. Распознано: scheme={parts.scheme or '—'}, host={detected_host or '—'}")
        if not _is_ozon_host(parts.hostname):
            raise ValueError(
                "Распознан URL, но домен не входит в разрешённые consumer-домены Ozon "
                f"(.ozon.ru/.ozon.kz). Распознан host={detected_host or '—'}"
            )
        if method != "GET":
            raise ValueError(f"Для лаборатории нужен GET поисковой выдачи Ozon, а в cURL метод {method}")
        decoded_url = unquote(url)
        if "searchsuggestions" in decoded_url.lower():
            raise ValueError("Это запрос подсказок searchSuggestions. Выбери v2-запрос с '/search/?', который грузит саму выдачу товаров")
        if "/search/" not in decoded_url.lower():
            raise ValueError("В запросе не найден /search/. Выбери Network-запрос, который загружает результаты поиска")

        return cls(url=url, headers=headers, cookie=cookie, method=method)

    @property
    def origin(self) -> str:
        p = urlsplit(self.url)
        return urlunsplit((p.scheme, p.netloc, "", "", "")).rstrip("/")

    def redacted_summary(self) -> dict:
        p = urlsplit(self.url)
        outer = dict(parse_qsl(p.query, keep_blank_values=True))
        inner = outer.get("url")
        cookie_count = 0
        if self.cookie:
            cookie_count = len([x for x in self.cookie.split(";") if x.strip() and "=" in x])
        return {
            "host": p.hostname,
            "origin": self.origin,
            "path": p.path,
            "has_inner_url": bool(inner),
            "inner_path": urlsplit(inner).path if inner else None,
            "header_names": sorted(k for k in self.headers.keys() if k.lower() not in SENSITIVE_HEADERS),
            "headers_count": len(self.headers),
            "cookies_present": bool(self.cookie),
            "cookies_count": cookie_count,
            "method": self.method,
        }

    def rewritten_search_url(self, query: str, page: int = 1) -> str:
        query = str(query or "").strip()
        if not query:
            raise ValueError("Введи название товара")
        page = max(1, int(page))

        outer = urlsplit(self.url)
        outer_pairs = list(parse_qsl(outer.query, keep_blank_values=True))
        replaced_outer: list[tuple[str, str]] = []
        found_inner = False
        for key, value in outer_pairs:
            if key == "url":
                found_inner = True
                inner = urlsplit(value)
                inner_pairs = list(parse_qsl(inner.query, keep_blank_values=True))
                out_inner: list[tuple[str, str]] = []
                seen_text = False
                seen_page = False
                for ikey, ivalue in inner_pairs:
                    if ikey == "text":
                        out_inner.append((ikey, query))
                        seen_text = True
                    elif ikey == "page":
                        out_inner.append((ikey, str(page)))
                        seen_page = True
                    else:
                        out_inner.append((ikey, ivalue))
                if not seen_text:
                    out_inner.append(("text", query))
                if not seen_page:
                    out_inner.append(("page", str(page)))
                new_inner = urlunsplit((inner.scheme, inner.netloc, inner.path, urlencode(out_inner, doseq=True), inner.fragment))
                replaced_outer.append((key, new_inner))
            else:
                replaced_outer.append((key, value))

        if not found_inner:
            # Fallback for a direct /search endpoint.
            direct_pairs = list(parse_qsl(outer.query, keep_blank_values=True))
            out_direct: list[tuple[str, str]] = []
            seen_text = seen_page = False
            for key, value in direct_pairs:
                if key == "text":
                    out_direct.append((key, query)); seen_text = True
                elif key == "page":
                    out_direct.append((key, str(page))); seen_page = True
                else:
                    out_direct.append((key, value))
            if not seen_text:
                out_direct.append(("text", query))
            if not seen_page:
                out_direct.append(("page", str(page)))
            return urlunsplit((outer.scheme, outer.netloc, outer.path, urlencode(out_direct, doseq=True), outer.fragment))

        return urlunsplit((outer.scheme, outer.netloc, outer.path, urlencode(replaced_outer, doseq=True), outer.fragment))

    def rewritten_inner_url(self, inner_target: str) -> str:
        """Reuse the accepted entrypoint/composer endpoint for an arbitrary Ozon inner route.

        `inner_target` must be a same-site relative path such as
        `/modal/otherOffersFromSellers?...`.  This keeps the HTTP transport,
        host and accepted browser session from the imported Chrome cURL.
        """
        inner_target = str(inner_target or "").strip()
        if not inner_target.startswith("/") or inner_target.startswith("//"):
            raise ValueError("Нужен относительный Ozon inner URL, начинающийся с /")
        outer = urlsplit(self.url)
        pairs = list(parse_qsl(outer.query, keep_blank_values=True))
        if any(k == "url" for k, _ in pairs):
            pairs = [(k, inner_target if k == "url" else v) for k, v in pairs]
            return urlunsplit((outer.scheme, outer.netloc, outer.path, urlencode(pairs, doseq=True), outer.fragment))
        # Imported request should normally be entrypoint/composer.  Keep a
        # safe fallback for direct requests on the same Ozon origin.
        return self.origin + inner_target

    def rewritten_page_url(self, target_url: str) -> str:
        """Reuse the working entrypoint/composer request for an Ozon product page."""
        target = urlsplit(str(target_url or "").strip())
        if target.scheme.lower() != "https" or not _is_ozon_host(target.hostname):
            raise ValueError("Для detail-probe нужен HTTPS URL Ozon")
        inner_target = urlunsplit(("", "", target.path or "/", target.query, target.fragment))
        outer = urlsplit(self.url)
        pairs = list(parse_qsl(outer.query, keep_blank_values=True))
        if any(k == "url" for k, _ in pairs):
            pairs = [(k, inner_target if k == "url" else v) for k, v in pairs]
            return urlunsplit((outer.scheme, outer.netloc, outer.path, urlencode(pairs, doseq=True), outer.fragment))
        return str(target_url)

    def request_headers_for_page(self, target_url: str) -> dict[str, str]:
        headers = self.request_headers()
        for key in list(headers.keys()):
            if key.lower() == "referer":
                headers[key] = str(target_url)
        return headers

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "headers": dict(self.headers),
            "cookie": self.cookie,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "CurlProfile":
        if not isinstance(value, dict):
            raise ValueError("Некорректный сохранённый session profile")
        profile = cls(
            url=str(value.get("url") or ""),
            headers={str(k): str(v) for k, v in dict(value.get("headers") or {}).items()},
            cookie=(str(value.get("cookie")) if value.get("cookie") is not None else None),
            method=str(value.get("method") or "GET").upper(),
        )
        parts = urlsplit(profile.url)
        if parts.scheme.lower() != "https" or not _is_ozon_host(parts.hostname):
            raise ValueError("Сохранённый profile содержит недопустимый Ozon URL")
        if profile.method != "GET":
            raise ValueError("Сохранённый profile должен использовать GET")
        return profile

    def merge_cookie_values(self, updates: dict[str, str]) -> int:
        if not updates:
            return 0
        ordered: list[tuple[str, str]] = []
        positions: dict[str, int] = {}
        for chunk in str(self.cookie or "").split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name = name.strip()
            if not name:
                continue
            positions[name] = len(ordered)
            ordered.append((name, value.strip()))
        changed = 0
        for raw_name, raw_value in updates.items():
            name = str(raw_name or "").strip()
            if not name:
                continue
            value = str(raw_value or "")
            if name in positions:
                idx = positions[name]
                if ordered[idx][1] != value:
                    ordered[idx] = (name, value)
                    changed += 1
            else:
                positions[name] = len(ordered)
                ordered.append((name, value))
                changed += 1
        self.cookie = "; ".join(f"{name}={value}" for name, value in ordered) or None
        return changed

    def request_headers(self) -> dict[str, str]:
        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP_HEADERS and k.lower() != "cookie"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def request_headers_for_search(self, query: str, page: int = 1) -> dict[str, str]:
        headers = self.request_headers()
        # Browser changes Referer together with the visible search page. Keep every other
        # copied header byte-for-byte and only rewrite text/page in a search referer.
        for key in list(headers.keys()):
            if key.lower() != "referer":
                continue
            ref = headers[key]
            try:
                parts = urlsplit(ref)
                if "/search/" not in parts.path.lower():
                    continue
                pairs = list(parse_qsl(parts.query, keep_blank_values=True))
                out: list[tuple[str, str]] = []
                seen_text = seen_page = False
                for rkey, value in pairs:
                    if rkey == "text":
                        out.append((rkey, query)); seen_text = True
                    elif rkey == "page":
                        out.append((rkey, str(max(1, int(page))))); seen_page = True
                    else:
                        out.append((rkey, value))
                if not seen_text:
                    out.append(("text", query))
                if int(page) > 1 and not seen_page:
                    out.append(("page", str(max(1, int(page)))))
                headers[key] = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(out, doseq=True), parts.fragment))
            except Exception:
                pass
        return headers
