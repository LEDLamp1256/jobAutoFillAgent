"""Pure parser for the Simplify Summer 2027 machine-readable feed."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlsplit

from .domain import DiscoveredListing, RegistrationIncompatible, RegistrationIssue


SOURCE_KEY = "simplify_summer2027"
SOURCE_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
    "dev/.github/scripts/listings.json"
)


class Classification(str, Enum):
    MALFORMED_JSON = "malformed_json"
    WRONG_ROOT = "wrong_root"
    NON_OBJECT_ROW = "non_object_row"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_FIELD_TYPE = "invalid_field_type"
    BLANK_REQUIRED_STRING = "blank_required_string"
    INVALID_URL = "invalid_url"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_LOCATIONS = "invalid_locations"
    INACTIVE = "inactive"
    HIDDEN = "hidden"


@dataclass(frozen=True)
class SimplifyMetadata:
    contributor_source: str | None
    terms: tuple[str, ...]
    sponsorship: str | None
    category: str | None
    degrees: tuple[str, ...]
    company_url: str | None


@dataclass(frozen=True)
class SimplifyDiscoveredListing:
    listing: DiscoveredListing
    metadata: SimplifyMetadata


@dataclass(frozen=True)
class ParseResult:
    raw_row_count: int
    source_valid_count: int
    eligible: tuple[SimplifyDiscoveredListing, ...]
    classification_counts: tuple[tuple[Classification, int], ...]
    registration_compatible_count: int
    registration_incompatible_counts: tuple[tuple[RegistrationIssue, int], ...]

    @property
    def eligible_count(self) -> int:
        return len(self.eligible)


class _RowInvalid(Exception):
    def __init__(self, reason: Classification):
        self.reason = reason


def _required_string(row: dict, key: str) -> str:
    if key not in row:
        raise _RowInvalid(Classification.MISSING_REQUIRED_FIELD)
    value = row[key]
    if not isinstance(value, str):
        raise _RowInvalid(Classification.INVALID_FIELD_TYPE)
    if not value.strip():
        raise _RowInvalid(Classification.BLANK_REQUIRED_STRING)
    return value.strip()


def _optional_string(row: dict, key: str) -> str | None:
    value = row.get(key)
    if value is None and key not in row:
        return None
    if not isinstance(value, str):
        raise _RowInvalid(Classification.INVALID_FIELD_TYPE)
    return value.strip() or None


def _string_array(row: dict, key: str, *, required: bool = False) -> tuple[str, ...]:
    if key not in row:
        if required:
            raise _RowInvalid(Classification.MISSING_REQUIRED_FIELD)
        return ()
    value = row[key]
    reason = Classification.INVALID_LOCATIONS if key == "locations" else Classification.INVALID_FIELD_TYPE
    if not isinstance(value, list) or (required and not value):
        raise _RowInvalid(reason)
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise _RowInvalid(reason)
    return tuple(item.strip() for item in value)


def _timestamp(row: dict, key: str) -> datetime:
    if key not in row:
        raise _RowInvalid(Classification.MISSING_REQUIRED_FIELD)
    value = row[key]
    if type(value) is not int:
        raise _RowInvalid(Classification.INVALID_TIMESTAMP)
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise _RowInvalid(Classification.INVALID_TIMESTAMP) from None


def _posting_url(row: dict) -> str:
    url = _required_string(row, "url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or any(ch.isspace() or ord(ch) < 32 for ch in url)):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise _RowInvalid(Classification.INVALID_URL) from None
    return url


def _parse_row(row: dict) -> SimplifyDiscoveredListing:
    source_listing_id = _required_string(row, "id")
    company = _required_string(row, "company_name")
    title = _required_string(row, "title")
    url = _posting_url(row)
    locations = _string_array(row, "locations", required=True)
    posted = _timestamp(row, "date_posted")
    updated = _timestamp(row, "date_updated")
    for key in ("active", "is_visible"):
        if key not in row:
            raise _RowInvalid(Classification.MISSING_REQUIRED_FIELD)
        if type(row[key]) is not bool:
            raise _RowInvalid(Classification.INVALID_FIELD_TYPE)
    metadata = SimplifyMetadata(
        contributor_source=_optional_string(row, "source"),
        terms=_string_array(row, "terms"),
        sponsorship=_optional_string(row, "sponsorship"),
        category=_optional_string(row, "category"),
        degrees=_string_array(row, "degrees"),
        company_url=_optional_string(row, "company_url"),
    )
    return SimplifyDiscoveredListing(
        listing=DiscoveredListing(SOURCE_KEY, source_listing_id, company, title, url,
                                  locations, posted, updated),
        metadata=metadata,
    )


def parse_simplify_listings(payload: bytes) -> ParseResult:
    """Classify source rows without network access or durable side effects."""
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return ParseResult(0, 0, (), ((Classification.MALFORMED_JSON, 1),), 0, ())
    if not isinstance(data, list):
        return ParseResult(0, 0, (), ((Classification.WRONG_ROOT, 1),), 0, ())

    counts: Counter[Classification] = Counter()
    incompatible: Counter[RegistrationIssue] = Counter()
    eligible: list[SimplifyDiscoveredListing] = []
    source_valid_count = compatible_count = 0
    for row in data:
        if not isinstance(row, dict):
            counts[Classification.NON_OBJECT_ROW] += 1
            continue
        try:
            discovered = _parse_row(row)
        except _RowInvalid as error:
            counts[error.reason] += 1
            continue
        source_valid_count += 1
        if not row["active"]:
            counts[Classification.INACTIVE] += 1
            continue
        if not row["is_visible"]:
            counts[Classification.HIDDEN] += 1
            continue
        eligible.append(discovered)
        try:
            discovered.listing.to_listing_input()
        except RegistrationIncompatible as error:
            incompatible[error.reason] += 1
        else:
            compatible_count += 1
    return ParseResult(
        len(data), source_valid_count, tuple(eligible),
        tuple(sorted(counts.items(), key=lambda item: item[0].value)),
        compatible_count,
        tuple(sorted(incompatible.items(), key=lambda item: item[0].value)),
    )
