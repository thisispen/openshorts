"""Source URL parsing: platform detection, canonical form and a syntactic safety check.

Research ingest never fetches these URLs, so the check is syntactic (scheme,
credentials, IP literals). Anything that does fetch must still go through
`security_utils.assert_public_url` and `ssrf_guard.guarded()`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048

_IG_PATH = re.compile(r"^/(?:[A-Za-z0-9_.]+/)?(reel|reels|p|tv)/([A-Za-z0-9_-]{5,64})/?$")
_YT_SHORTS = re.compile(r"^/shorts/([A-Za-z0-9_-]{11})/?$")
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TT_VIDEO = re.compile(r"^/@([A-Za-z0-9_.]{1,64})/video/(\d{5,25})/?$")


class UnsafeUrlError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedSource:
    platform: str  # instagram | youtube | tiktok | other
    content_type: str  # reel | post | short | video | unknown
    external_id: str | None
    canonical_url: str
    creator_handle: str | None = None


def _check_host(host: str) -> None:
    if not host or "." not in host:
        raise UnsafeUrlError("URL host must be a public domain name")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
            raise UnsafeUrlError("URL host must be a public domain name")
        return
    raise UnsafeUrlError(f"IP-literal hosts are not accepted ({ip})")


def parse_source_url(raw: str) -> ParsedSource:
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise UnsafeUrlError("URL must be 1-2048 characters")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only http(s) URLs are accepted")
    if parts.username or parts.password:
        raise UnsafeUrlError("URLs with embedded credentials are not accepted")
    host = (parts.hostname or "").lower()
    _check_host(host)
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    path = parts.path or "/"

    if host in {"instagram.com", "instagr.am"}:
        match = _IG_PATH.match(path)
        if match:
            kind, code = match.groups()
            content_type = "reel" if kind in {"reel", "reels", "tv"} else "post"
            segment = "reel" if content_type == "reel" else "p"
            return ParsedSource(
                "instagram", content_type, code, f"https://www.instagram.com/{segment}/{code}/"
            )
        handle = path.strip("/").split("/")[0] if path.strip("/") else None
        canonical = f"https://www.instagram.com/{handle}/" if handle else "https://www.instagram.com/"
        return ParsedSource("instagram", "unknown", None, canonical, handle)

    if host in {"youtube.com", "youtu.be"}:
        video_id = None
        content_type = "video"
        shorts = _YT_SHORTS.match(path)
        if shorts:
            video_id, content_type = shorts.group(1), "short"
        elif host == "youtu.be":
            candidate = path.strip("/")
            video_id = candidate if _YT_ID.match(candidate) else None
        elif path == "/watch":
            for pair in parts.query.split("&"):
                key, _, value = pair.partition("=")
                if key == "v" and _YT_ID.match(value):
                    video_id = value
        if video_id:
            prefix = "shorts/" if content_type == "short" else "watch?v="
            return ParsedSource("youtube", content_type, video_id, f"https://www.youtube.com/{prefix}{video_id}")
        return ParsedSource("youtube", "unknown", None, f"https://www.youtube.com{path.rstrip('/')}")

    if host == "tiktok.com":
        match = _TT_VIDEO.match(path)
        if match:
            handle, video_id = match.groups()
            return ParsedSource(
                "tiktok", "video", video_id, f"https://www.tiktok.com/@{handle}/video/{video_id}", handle
            )

    # Unknown sites keep their path but lose query strings and fragments, which
    # mostly carry tracking parameters.
    return ParsedSource("other", "unknown", None, f"https://{host}{path.rstrip('/') or '/'}")
