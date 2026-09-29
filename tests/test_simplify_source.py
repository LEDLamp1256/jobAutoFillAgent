"""Offline contract tests for the Simplify discovery boundary."""

import json
import unittest
from dataclasses import fields
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from jobagent.batch_domain import JobListing
from jobagent.dedupe import ListingInput
from jobagent.sources.domain import RegistrationIncompatible, RegistrationIssue
from jobagent.sources.simplify import Classification, SOURCE_KEY, parse_simplify_listings
from jobagent.sources.simplify_diagnostic import FetchError, fetch_simplify_payload, live_diagnostic


def row(**changes):
    item = {
        "source": "upstream-contributor",
        "company_name": "Acme",
        "id": "upstream-123",
        "title": "Engineering Intern",
        "active": True,
        "date_updated": 1790000060,
        "is_visible": True,
        "date_posted": 1790000000,
        "url": "https://jobs.example.test/req/123?job=123",
        "locations": ["San Francisco, CA", "New York, NY"],
        "company_url": "",
        "terms": ["Summer 2027"],
        "sponsorship": "Other",
        "category": "Software",
        "degrees": ["Bachelor's"],
    }
    item.update(changes)
    return item


def parse(*items):
    return parse_simplify_listings(json.dumps(items).encode("utf-8"))


def reasons(result):
    return dict(result.classification_counts)


class SimplifySourceTests(unittest.TestCase):
    def test_valid_listing_and_explicit_conversion(self):
        result = parse(row())
        self.assertEqual((result.raw_row_count, result.source_valid_count,
                          result.eligible_count, result.registration_compatible_count), (1, 1, 1, 1))
        discovered = result.eligible[0]
        listing = discovered.listing
        self.assertEqual(listing.source, SOURCE_KEY)
        self.assertEqual(listing.source_listing_id, "upstream-123")
        self.assertEqual(listing.locations, ("San Francisco, CA", "New York, NY"))
        self.assertEqual(listing.source_posted_at,
                         datetime.fromtimestamp(1790000000, timezone.utc))
        self.assertEqual(listing.source_updated_at,
                         datetime.fromtimestamp(1790000060, timezone.utc))
        converted = listing.to_listing_input()
        self.assertIsInstance(converted, ListingInput)
        self.assertEqual(converted.source, SOURCE_KEY)
        self.assertEqual(converted.source_listing_id, "upstream-123")
        self.assertEqual(converted.location, "San Francisco, CA; New York, NY")
        self.assertEqual(converted.listing_url, listing.listing_url)
        self.assertIsNone(converted.application_url)
        self.assertEqual(discovered.metadata.contributor_source, "upstream-contributor")
        self.assertEqual(discovered.metadata.terms, ("Summer 2027",))
        self.assertEqual(discovered.metadata.degrees, ("Bachelor's",))
        self.assertFalse(set(("terms", "sponsorship", "category", "degrees")) &
                         {field.name for field in fields(JobListing)})

    def test_inactive_and_hidden_are_source_valid_but_not_eligible(self):
        result = parse(row(id="inactive", active=False),
                       row(id="hidden", is_visible=False), row(id="both", active=False, is_visible=False))
        self.assertEqual((result.source_valid_count, result.eligible_count), (3, 0))
        self.assertEqual(reasons(result)[Classification.INACTIVE], 2)
        self.assertEqual(reasons(result)[Classification.HIDDEN], 1)

    def test_malformed_json_and_wrong_root_are_bounded_results(self):
        invalid = parse_simplify_listings(b"{")
        self.assertEqual(invalid.classification_counts, ((Classification.MALFORMED_JSON, 1),))
        wrong = parse_simplify_listings(b'{"listings": []}')
        self.assertEqual(wrong.classification_counts, ((Classification.WRONG_ROOT, 1),))
        self.assertEqual((invalid.raw_row_count, wrong.raw_row_count), (0, 0))

    def test_non_object_and_missing_required_field(self):
        missing = row()
        del missing["company_name"]
        result = parse(None, missing)
        self.assertEqual(result.raw_row_count, 2)
        self.assertEqual(reasons(result)[Classification.NON_OBJECT_ROW], 1)
        self.assertEqual(reasons(result)[Classification.MISSING_REQUIRED_FIELD], 1)

    def test_blank_required_value_and_wrong_scalar_type(self):
        result = parse(row(id=" "), row(company_name=5), row(active="true"))
        self.assertEqual(reasons(result)[Classification.BLANK_REQUIRED_STRING], 1)
        self.assertEqual(reasons(result)[Classification.INVALID_FIELD_TYPE], 2)

    def test_wrong_array_and_empty_or_invalid_locations(self):
        result = parse(row(locations="Remote"), row(locations=[]),
                       row(locations=["Remote", " "]), row(terms="Summer 2027"))
        self.assertEqual(reasons(result)[Classification.INVALID_LOCATIONS], 3)
        self.assertEqual(reasons(result)[Classification.INVALID_FIELD_TYPE], 1)

    def test_invalid_urls_do_not_become_discovered_listings(self):
        result = parse(row(url="ftp://jobs.example.test/123"), row(url="not-a-url"),
                       row(url="https://jobs.example.test/a b"))
        self.assertEqual(reasons(result)[Classification.INVALID_URL], 3)

    def test_timestamp_validation_accepts_old_dates_but_not_bad_types_or_ranges(self):
        result = parse(row(date_posted=0), row(date_posted=True),
                       row(date_updated="1790000060"), row(date_posted=10**30))
        self.assertEqual(result.eligible_count, 1)
        self.assertEqual(result.eligible[0].listing.source_posted_at,
                         datetime.fromtimestamp(0, timezone.utc))
        self.assertEqual(reasons(result)[Classification.INVALID_TIMESTAMP], 3)

    def test_unknown_keys_and_missing_optional_metadata_are_tolerated(self):
        item = row(new_upstream_field={"unknown": True})
        for key in ("source", "terms", "sponsorship", "category", "degrees", "company_url"):
            del item[key]
        result = parse(item)
        self.assertEqual(result.eligible_count, 1)
        self.assertIsNone(result.eligible[0].metadata.contributor_source)
        self.assertEqual(result.eligible[0].metadata.terms, ())

    def test_same_payload_has_identical_results(self):
        payload = json.dumps([row(), row(id="closed", active=False)]).encode()
        self.assertEqual(parse_simplify_listings(payload), parse_simplify_listings(payload))

    def test_token_url_is_eligible_but_registration_incompatible_without_rewrite(self):
        url = "https://jobs.example.test/req/123?token=example-secret&job=123"
        result = parse(row(url=url))
        self.assertEqual((result.source_valid_count, result.eligible_count,
                          result.registration_compatible_count), (1, 1, 0))
        self.assertEqual(result.registration_incompatible_counts,
                         ((RegistrationIssue.URL_POLICY, 1),))
        self.assertEqual(result.eligible[0].listing.listing_url, url)
        with self.assertRaises(RegistrationIncompatible) as error:
            result.eligible[0].listing.to_listing_input()
        self.assertEqual(error.exception.reason, RegistrationIssue.URL_POLICY)
        self.assertNotIn("example-secret", str(error.exception))

    def test_diagnostic_contains_counts_and_no_listing_urls(self):
        payload = json.dumps([row(url="https://jobs.example.test/123?token=example-secret"),
                              row(id="closed", active=False)]).encode()
        with patch("jobagent.sources.simplify_diagnostic.fetch_simplify_payload",
                   return_value=(200, payload)):
            diagnostic = live_diagnostic()
        self.assertEqual(diagnostic["eligible_count"], 1)
        self.assertEqual(diagnostic["inactive_count"], 1)
        self.assertEqual(diagnostic["registration_incompatible_counts"], {"url_policy": 1})
        self.assertNotIn("example-secret", json.dumps(diagnostic))

    def test_live_fetch_enforces_stream_size_without_network(self):
        response = MagicMock()
        response.status_code = 200
        response.url = "https://raw.githubusercontent.com/feed.json"
        response.headers = {}
        response.iter_content.return_value = (b"12345", b"6789")
        response.__enter__.return_value = response
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        with patch("jobagent.sources.simplify_diagnostic.requests.Session", return_value=session), \
             patch("jobagent.sources.simplify_diagnostic.MAX_PAYLOAD_BYTES", 8):
            with self.assertRaises(FetchError):
                fetch_simplify_payload()
        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.kwargs["timeout"], (5, 30))
        self.assertTrue(session.get.call_args.kwargs["stream"])

    def test_live_diagnostic_rejects_invalid_payload_without_network(self):
        with patch("jobagent.sources.simplify_diagnostic.fetch_simplify_payload",
                   return_value=(200, b"{")):
            with self.assertRaises(FetchError):
                live_diagnostic()


if __name__ == "__main__":
    unittest.main()
