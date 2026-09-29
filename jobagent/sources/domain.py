"""Normalized discovery values, separate from durable listings and tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from jobagent.dedupe import ListingInput


class RegistrationIssue(str, Enum):
    URL_POLICY = "url_policy"


class RegistrationIncompatible(ValueError):
    def __init__(self, reason: RegistrationIssue):
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class DiscoveredListing:
    source: str
    source_listing_id: str
    company: str
    title: str
    listing_url: str
    locations: tuple[str, ...]
    source_posted_at: datetime
    source_updated_at: datetime

    def to_listing_input(self) -> ListingInput:
        """Ask the existing domain contract to validate registration compatibility."""
        try:
            return ListingInput(
                source=self.source,
                source_listing_id=self.source_listing_id,
                company=self.company,
                title=self.title,
                listing_url=self.listing_url,
                location="; ".join(self.locations),
                application_url=None,
            )
        except ValueError as error:
            raise RegistrationIncompatible(RegistrationIssue.URL_POLICY) from error
