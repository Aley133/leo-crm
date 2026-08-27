from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image, ImageOps

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def _bits_to_similarity(a: int, b: int, bits: int = 64) -> float:
    return 1.0 - ((a ^ b).bit_count() / bits)


def _ahash(img: Image.Image) -> int:
    gray = ImageOps.grayscale(img).resize((8, 8), Image.Resampling.LANCZOS)
    px = list(gray.getdata())
    avg = sum(px) / len(px)
    out = 0
    for value in px:
        out = (out << 1) | int(value >= avg)
    return out


def _dhash(img: Image.Image) -> int:
    gray = ImageOps.grayscale(img).resize((9, 8), Image.Resampling.LANCZOS)
    px = list(gray.getdata())
    out = 0
    for y in range(8):
        row = px[y * 9:(y + 1) * 9]
        for x in range(8):
            out = (out << 1) | int(row[x] >= row[x + 1])
    return out


def _hist(img: Image.Image) -> list[float]:
    small = img.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
    hist = small.histogram()
    # Collapse 256 bins per channel into 32 bins per channel.
    out: list[float] = []
    for channel in range(3):
        base = channel * 256
        for bucket in range(32):
            start = base + bucket * 8
            out.append(float(sum(hist[start:start + 8])))
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


def _hist_similarity(a: list[float], b: list[float]) -> float:
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def _fingerprint(img: Image.Image) -> dict[str, Any]:
    img = ImageOps.exif_transpose(img).convert("RGB")
    return {
        "ahash": _ahash(img),
        "dhash": _dhash(img),
        "hist": _hist(img),
    }


def _compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, float]:
    ah = _bits_to_similarity(int(a["ahash"]), int(b["ahash"]))
    dh = _bits_to_similarity(int(a["dhash"]), int(b["dhash"]))
    hs = _hist_similarity(list(a["hist"]), list(b["hist"]))
    # Perceptual hashes dominate. Histogram is only a weak supporting signal.
    combined = max(0.0, min(1.0, ah * 0.42 + dh * 0.48 + hs * 0.10))
    return {"ahash": round(ah, 3), "dhash": round(dh, 3), "hist": round(hs, 3), "score": round(combined, 3)}


@dataclass
class ImageVerifier:
    timeout: float = 5.0
    max_bytes: int = 6_000_000

    def __post_init__(self) -> None:
        self.client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
        )
        self._cache: dict[str, dict[str, Any] | None] = {}

    def close(self) -> None:
        self.client.close()

    def _load(self, url: str) -> dict[str, Any] | None:
        url = str(url or "").strip()
        if not url.startswith("http"):
            return None
        if url in self._cache:
            return self._cache[url]
        try:
            with self.client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    self._cache[url] = None
                    return None
                content = bytearray()
                for chunk in resp.iter_bytes():
                    content.extend(chunk)
                    if len(content) > self.max_bytes:
                        self._cache[url] = None
                        return None
            img = Image.open(io.BytesIO(bytes(content)))
            fp = _fingerprint(img)
        except Exception:
            fp = None
        self._cache[url] = fp
        return fp

    def verify(self, kaspi_urls: list[str] | None, ozon_urls: list[str] | None, max_pairs: int = 6) -> dict[str, Any]:
        k_urls = [str(x) for x in (kaspi_urls or []) if str(x).startswith("http")][:3]
        o_urls = [str(x) for x in (ozon_urls or []) if str(x).startswith("http")][:3]
        if not k_urls or not o_urls:
            return {"status": "UNAVAILABLE", "score": None, "pairs_checked": 0, "reason": "нет фото одной из сторон"}

        best: dict[str, Any] | None = None
        pairs = 0
        for ku in k_urls:
            kfp = self._load(ku)
            if not kfp:
                continue
            for ou in o_urls:
                ofp = self._load(ou)
                if not ofp:
                    continue
                pairs += 1
                scores = _compare(kfp, ofp)
                row = {**scores, "kaspi_url": ku, "ozon_url": ou}
                if best is None or row["score"] > best["score"]:
                    best = row
                if pairs >= max_pairs:
                    break
            if pairs >= max_pairs:
                break

        if best is None:
            return {"status": "UNAVAILABLE", "score": None, "pairs_checked": pairs, "reason": "не удалось скачать/прочитать фото"}
        score = float(best["score"])
        if score >= 0.90:
            status = "CONFIRM"
        elif score >= 0.80:
            status = "SUPPORT"
        else:
            # IMPORTANT: different marketplace photos often use different crops,
            # so a low score is never treated as a rejection by itself.
            status = "UNKNOWN"
        return {
            "status": status,
            "score": round(score, 3),
            "pairs_checked": pairs,
            "components": {k: best[k] for k in ("ahash", "dhash", "hist")},
            "reason": "визуальное совпадение подтверждает title" if status in {"CONFIRM", "SUPPORT"} else "фото не похоже достаточно сильно для подтверждения",
        }
