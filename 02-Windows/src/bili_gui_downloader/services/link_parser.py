from __future__ import annotations

import re


URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
BV_PATTERN = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
AV_PATTERN = re.compile(r"\bav\d+\b", re.IGNORECASE)
TRAILING_PUNCTUATION = "\"'<>[](){}，。；;、,"


def extract_video_urls(raw_text: str) -> list[str]:
    found_urls = [match.group(0).strip().rstrip(TRAILING_PUNCTUATION) for match in URL_PATTERN.finditer(raw_text)]
    candidates = found_urls[:]

    for match in BV_PATTERN.finditer(raw_text):
        candidates.append(f"https://www.bilibili.com/video/{match.group(0)}/")

    for match in AV_PATTERN.finditer(raw_text):
        candidates.append(f"https://www.bilibili.com/video/{match.group(0).lower()}/")

    normalized: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        cleaned = normalize_video_url(url)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def normalize_video_url(url: str) -> str:
    text = (url or "").strip().rstrip(TRAILING_PUNCTUATION)
    if not text:
        return ""

    bv_match = BV_PATTERN.search(text)
    if bv_match:
        return f"https://www.bilibili.com/video/{bv_match.group(0)}/"

    av_match = AV_PATTERN.search(text)
    if av_match:
        return f"https://www.bilibili.com/video/{av_match.group(0).lower()}/"

    return text
