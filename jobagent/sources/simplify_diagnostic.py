"""Explicit read-only live diagnostic for the Simplify source feed."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from .simplify import Classification, SOURCE_KEY, SOURCE_URL, parse_simplify_listings


MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_FETCH_SECONDS = 60


class FetchError(RuntimeError):
    """A bounded public feed fetch could not complete."""


def fetch_simplify_payload() -> tuple[int, bytes]:
    """Fetch only when explicitly called; keep transport outside the parser."""
    deadline = time.monotonic() + MAX_FETCH_SECONDS
    with requests.Session() as session:
        session.max_redirects = 3
        with session.get(SOURCE_URL, stream=True, timeout=(5, 30), allow_redirects=True) as response:
            if response.status_code != 200:
                raise FetchError("unexpected_http_status")
            if urlsplit(response.url).scheme != "https":
                raise FetchError("non_https_redirect")
            declared_size = response.headers.get("Content-Length")
            if declared_size is not None:
                try:
                    if int(declared_size) > MAX_PAYLOAD_BYTES:
                        raise FetchError("payload_too_large")
                except ValueError:
                    raise FetchError("invalid_content_length") from None
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() > deadline:
                    raise FetchError("fetch_deadline_exceeded")
                if len(data) + len(chunk) > MAX_PAYLOAD_BYTES:
                    raise FetchError("payload_too_large")
                data.extend(chunk)
            return response.status_code, bytes(data)


def live_diagnostic() -> dict[str, object]:
    status, payload = fetch_simplify_payload()
    fetched_at = datetime.now(timezone.utc).isoformat()
    result = parse_simplify_listings(payload)
    if result.classification_counts in (
        ((Classification.MALFORMED_JSON, 1),),
        ((Classification.WRONG_ROOT, 1),),
    ):
        raise FetchError("invalid_payload")
    reasons = dict(result.classification_counts)
    return {
        "source": SOURCE_KEY,
        "source_url": SOURCE_URL,
        "fetched_at": fetched_at,
        "http_status": status,
        "payload_bytes": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "raw_row_count": result.raw_row_count,
        "source_valid_count": result.source_valid_count,
        "eligible_count": result.eligible_count,
        "inactive_count": reasons.get(Classification.INACTIVE, 0),
        "hidden_count": reasons.get(Classification.HIDDEN, 0),
        "classification_counts": {reason.value: count for reason, count in result.classification_counts},
        "registration_compatible_count": result.registration_compatible_count,
        "registration_incompatible_counts": {
            reason.value: count for reason, count in result.registration_incompatible_counts
        },
    }


def main() -> int:
    try:
        result = live_diagnostic()
    except (requests.RequestException, FetchError):
        print(json.dumps({"source": SOURCE_KEY, "error": "diagnostic_failed"}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
