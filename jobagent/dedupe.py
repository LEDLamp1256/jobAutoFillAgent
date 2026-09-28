"""Conservative listing identities for cross-run deduplication."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_KEYS = {"fbclid", "gclid", "gbraid", "wbraid", "mc_cid", "mc_eid"}
_SECRET_KEYS = {"token", "access_token", "auth_token", "session", "session_id", "password", "secret"}


def canonicalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("identity URL must be an absolute HTTP(S) URL without userinfo")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host + (f":{port}" if port and not (parts.scheme.lower(), port) in
                     {("http", 80), ("https", 443)} else "")
    parameters = parse_qsl(parts.query, keep_blank_values=True)
    if any(key.casefold() in _SECRET_KEYS for key, _ in parameters):
        raise ValueError("identity URL contains a credential-like query parameter")
    query = urlencode([(key, value) for key, value in parameters
                       if key.casefold() not in _TRACKING_KEYS and
                       not key.casefold().startswith("utm_")])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def _norm(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


@dataclass(frozen=True)
class ListingInput:
    source: str
    company: str
    title: str
    listing_url: str | None = None
    source_listing_id: str | None = None
    location: str | None = None
    application_url: str | None = None

    def __post_init__(self) -> None:
        if not all((self.source.strip(), self.company.strip(), self.title.strip())):
            raise ValueError("source, company, and title are required")
        canonicalize_url(self.listing_url)
        canonicalize_url(self.application_url)


class DuplicateKind(str, Enum):
    EXACT_DUPLICATE = "exact_duplicate"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    NEW = "new"


@dataclass(frozen=True)
class DuplicateResult:
    kind: DuplicateKind
    listing_id: str | None = None
    submitted: bool = False
    reason: str | None = None


def identities(item: ListingInput) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    if item.source_listing_id and item.source_listing_id.strip():
        result.append(("source_id", f"{_norm(item.source)}\0{item.source_listing_id.strip()}"))
    if item.listing_url:
        result.append(("listing_url", canonicalize_url(item.listing_url) or ""))
    if item.application_url:
        result.append(("application_url", canonicalize_url(item.application_url) or ""))
    # Incomplete posting metadata is too weak to declare an exact match.
    if not result and _norm(item.location):
        material = "\0".join((_norm(item.company), _norm(item.title), _norm(item.location)))
        result.append(("fallback", hashlib.sha256(material.encode("utf-8")).hexdigest()))
    return tuple(result)


def similarity_key(item: ListingInput) -> tuple[str, str]:
    return _norm(item.company), _norm(item.title)
