"""
Drives SEARCH -> NAVIGATION -> INTERACTION -> ERROR across a list of job
postings, reusing the existing scrape -> match -> inject -> review pipeline
(main.py / aiMatcher.py / injection.py / review.py) for each individual
application rather than reimplementing any of that logic.

INTEGRATION: INTERACTION_STATE calls main.py's run_scrape_stage(page),
run_match_stage(fields, config_path, model), run_injection_stage(page,
match_data), and run_review_stage(page, injection_report) directly — the
same functions main.py's single-form pipeline uses. Nothing about
scraping, matching, or injecting is reimplemented here.
"""

from __future__ import annotations

import argparse
import re
import sys
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
    sync_playwright,
)

import aiMatcher as aiMatcher_module
from review import find_and_wait_for_submit, log_application

from main import (
    InjectionError,
    MatchError,
    ReviewError,
    ScrapeError,
    run_injection_stage,
    run_match_stage,
    run_review_stage,
    run_scrape_stage,
)

logger = logging.getLogger("job_navigator")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# --------------------------------------------------------------------------- #
# State machine definitions
# --------------------------------------------------------------------------- #

class ApplicationState(str, Enum):
    SEARCH = "SEARCH"
    NAVIGATION = "NAVIGATION"
    INTERACTION = "INTERACTION"
    ERROR = "ERROR"
    DONE = "DONE"


class ApplyType(str, Enum):
    EASY_APPLY = "EASY_APPLY"
    EXTERNAL = "EXTERNAL"
    UNKNOWN = "UNKNOWN"


@dataclass
class JobListing:
    url: str
    title: str = ""


def _build_criteria_from_config(config_path: str) -> str:
    """
    Builds a plain-language criteria string from config.json's
    application_preferences section, so job-search preferences live in
    one place instead of being duplicated at the CLI. Raises ValueError
    if the config can't be loaded or has nothing usable, so the caller
    can fail with a clear message rather than silently filtering nothing.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError as e:
        raise ValueError(f"Config file not found at '{config_path}': {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"Config file at '{config_path}' is not valid JSON: {e}") from e

    prefs = config.get("application_preferences", {})
    if not isinstance(prefs, dict) or not prefs:
        raise ValueError(
            f"No 'application_preferences' section found in {config_path}. "
            f"Either add one (job_titles_of_interest, industries_of_interest, "
            f"remote_preference, excluded_companies, min_acceptable_salary), "
            f"or pass --criteria explicitly."
        )

    parts: list[str] = []

    titles = prefs.get("job_titles_of_interest") or []
    if titles:
        parts.append(f"Interested in roles titled: {', '.join(titles)}.")

    industries = prefs.get("industries_of_interest") or []
    if industries:
        parts.append(f"Preferred industries: {', '.join(industries)}.")

    remote_pref = prefs.get("remote_preference")
    if remote_pref:
        parts.append(f"Work arrangement preference: {remote_pref}.")

    min_salary = prefs.get("min_acceptable_salary")
    if min_salary:
        parts.append(f"Minimum acceptable salary: {min_salary}.")

    excluded = prefs.get("excluded_companies") or []
    if excluded:
        parts.append(
            f"Do NOT match listings that appear to be from these companies: "
            f"{', '.join(excluded)}."
        )

    if not parts:
        raise ValueError(
            f"'application_preferences' in {config_path} exists but has no "
            f"usable fields (job_titles_of_interest, industries_of_interest, "
            f"etc. are all empty). Populate it, or pass --criteria explicitly."
        )

    return " ".join(parts)


# Buttons that advance a multi-page application without submitting it.
NEXT_BUTTON_PRIORITY = [
    'text="Next"',
    'text="Continue"',
    'text="Save and continue"',
    '[aria-label="Continue to next step"]',
]

# If the button we land on contains any of these, treat it as the final
# submit -- route to the human review gate instead of auto-clicking it.
SUBMIT_LIKE_KEYWORDS = ("submit", "apply now", "send application", "finish application")

# Signals that an "Apply" button leaves the current site entirely.
EXTERNAL_APPLY_KEYWORDS = ("apply on company site", "external", "apply on employer site")

# Text patterns for a job's actual Apply/Quick-Apply trigger. Deliberately
# never falls back to a blind [role="button"]/[type="submit"] match (the
# way review.py's find_and_wait_for_submit does for an in-progress
# application form) — a job search/detail page has many unrelated
# role="button" elements (filter dropdowns, save/bookmark icons, share
# menus), and a blind match risks clicking one of those instead.
APPLY_BUTTON_TEXT_PATTERN = re.compile(
    r"quick apply|easy apply|apply now|^apply$|apply to this job|apply for this job",
    re.IGNORECASE,
)

# Safety ceiling so a broken "Next" loop can't run forever on one job.
MAX_PAGES_PER_APPLICATION = 10


# --------------------------------------------------------------------------- #
# Resumable state
# --------------------------------------------------------------------------- #

@dataclass
class NavigatorState:
    """
    Persisted to disk after every state transition so a crash mid-run can
    resume without re-scraping the search page or re-applying to jobs
    already processed. `processed` includes jobs that errored out too —
    a failed job is skipped, not retried forever.
    """
    search_url: str
    queue: list[str] = field(default_factory=list)
    processed: list[str] = field(default_factory=list)
    last_processed_url: Optional[str] = None
    updated_at: str = ""

    def save(self, path: Path) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["NavigatorState"]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Could not load resume state at {path}: {e}")
            return None


# --------------------------------------------------------------------------- #
# The Navigator
# --------------------------------------------------------------------------- #

class JobNavigator:
    """
    Agentic state machine driving scrape -> filter -> navigate -> apply
    across a list of job postings scraped from a single search URL.
    """

    def __init__(
        self,
        search_url: str,
        criteria: str | None = None,
        config_path: str = "config.json",
        model: str | None = None,
        max_applications: int | None = None,
        headless: bool = False,
        state_file: str = "navigator_state.json",
        min_delay_s: float = 2.0,
        max_delay_s: float = 5.0,
        open_external_in_new_tab: bool = False,
        resume_from_url: str | None = None,
        auth_state_path: str | None = None,
    ):
        self.search_url = search_url
        self.config_path = config_path

        # Explicit --criteria always wins; otherwise derive from
        # config.json's application_preferences.
        if criteria:
            self.criteria = criteria
        else:
            self.criteria = _build_criteria_from_config(config_path)
            logger.info(f"[SEARCH] No --criteria given; derived from {config_path}: {self.criteria}")

        self.model = model
        self.max_applications = max_applications
        self.headless = headless
        self.state_path = Path(state_file)
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s
        self.open_external_in_new_tab = open_external_in_new_tab
        self.resume_from_url = resume_from_url

        # Sites like Handshake require an authenticated session before
        # search results render at all. See login.py for how this saved
        # session file is created.
        self.auth_state_path = auth_state_path
        if auth_state_path and not Path(auth_state_path).exists():
            logger.warning(
                f"[SEARCH] --auth-state was given ({auth_state_path}) but that "
                f"file doesn't exist yet. Continuing without it -- run "
                f"login.py first if the target site requires login."
            )
            self.auth_state_path = None

        self.state: NavigatorState = NavigatorState.load(self.state_path) or NavigatorState(search_url=search_url)
        self.applications_submitted = 0
        self.current_state = ApplicationState.SEARCH

        self._playwright = None
        self._browser: Browser | None = None
        self._current_context: BrowserContext | None = None
        self._current_page: Page | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ #
    # Politeness delay -- not bot-detection evasion, see module docstring
    # ------------------------------------------------------------------ #
    def _polite_delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))

    # ------------------------------------------------------------------ #
    # Browser/context lifecycle -- fresh context per application
    # ------------------------------------------------------------------ #
    def _new_context(self) -> BrowserContext:
        """
        A fresh context per application for session hygiene (no cookie/
        login bleed between unrelated applications, no state corruption
        if job N crashes). If auth_state_path is set, each new context is
        seeded with that saved session via storage_state, so it starts
        already authenticated instead of needing a fresh manual login
        every time.
        """
        if self._current_context:
            try:
                self._current_context.close()
            except PlaywrightError:
                pass
        if self.auth_state_path:
            self._current_context = self._browser.new_context(storage_state=self.auth_state_path)
        else:
            self._current_context = self._browser.new_context()
        return self._current_context

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        with sync_playwright() as p:
            self._playwright = p
            self._browser = p.chromium.launch(headless=self.headless)
            try:
                while self.current_state != ApplicationState.DONE:
                    if self.max_applications is not None and self.applications_submitted >= self.max_applications:
                        logger.info(f"Reached max_applications={self.max_applications}. Stopping.")
                        self.current_state = ApplicationState.DONE
                        continue

                    if self.current_state == ApplicationState.SEARCH:
                        self._handle_search_state()
                    elif self.current_state == ApplicationState.NAVIGATION:
                        self._handle_navigation_state()
                    elif self.current_state == ApplicationState.INTERACTION:
                        self._handle_interaction_state()
                    elif self.current_state == ApplicationState.ERROR:
                        self._handle_error_state()
                    else:
                        logger.error(f"Unknown state: {self.current_state}")
                        self.current_state = ApplicationState.DONE
            finally:
                if self._current_context:
                    try:
                        self._current_context.close()
                    except PlaywrightError:
                        pass
                self._browser.close()
                logger.info(
                    f"=== JobNavigator finished. {self.applications_submitted} application(s) "
                    f"submitted, {len(self.state.processed)} total processed. Browser closed. ==="
                )

    # ------------------------------------------------------------------ #
    # SEARCH_STATE
    # ------------------------------------------------------------------ #
    def _wait_for_page_to_settle(
        self, page: Page, max_wait_s: float = 10.0, min_wait_s: float = 2.0,
        poll_interval_s: float = 0.5, stable_polls_required: int = 2,
    ) -> None:
        """
        Some SPAs fetch their job list via an async API call after initial
        load, then fade results in via CSS -- not reliably captured by
        page.wait_for_load_state("networkidle") alone, since that only
        tracks in-flight HTTP requests, not the render that follows, and
        a persistent background connection (analytics, websocket, chat
        widget) can prevent networkidle from firing at all.

        Polls the count of visible <a> elements until it stops growing
        across `stable_polls_required` consecutive checks, but only after
        min_wait_s has elapsed. The floor matters: without it, "nothing
        has arrived yet" and "everything has already arrived" look
        identical after a single poll.
        """
        deadline = time.monotonic() + max_wait_s
        floor_deadline = time.monotonic() + min_wait_s
        last_count = -1
        stable_polls = 0

        while time.monotonic() < deadline:
            try:
                count = page.evaluate(
                    "() => Array.from(document.querySelectorAll('a')).filter(a => {"
                    "  const r = a.getBoundingClientRect();"
                    "  return r.width > 0 && r.height > 0;"
                    "}).length"
                )
            except PlaywrightError:
                return

            past_floor = time.monotonic() >= floor_deadline
            if count == last_count:
                stable_polls += 1
                if past_floor and stable_polls >= stable_polls_required:
                    logger.info(f"[SEARCH] Page content stabilized at {count} visible link(s).")
                    return
            else:
                stable_polls = 0
            last_count = count
            page.wait_for_timeout(int(poll_interval_s * 1000))

        logger.info(
            f"[SEARCH] Page settle wait hit the {max_wait_s}s ceiling "
            f"(last count: {last_count}) -- proceeding with whatever rendered."
        )

    def _dump_repeated_structures(self, page: Page, top_n: int = 8) -> None:
        """
        Diagnostic only -- doesn't change scraping behavior. Finds
        whatever element pattern repeats the most on the page, regardless
        of tag, since job rows on master-detail SPAs are often clickable
        <div>/<li> rows with a JS onClick handler rather than real
        anchors, which no anchor-based selector could ever see.
        """
        try:
            groups = page.evaluate(r"""
                (topN) => {
                    function cleanText(el) {
                        return (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100);
                    }
                    function isVisible(el) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }
                    function signature(el) {
                        const cls = Array.from(el.classList).slice(0, 3).sort().join('.');
                        return el.tagName + (cls ? '.' + cls : '');
                    }

                    const candidates = Array.from(document.querySelectorAll(
                        'a, [role="button"], [role="listitem"], li, [data-testid], [onclick]'
                    )).filter(isVisible);

                    const groups = new Map();
                    for (const el of candidates) {
                        const parent = el.parentElement;
                        if (!parent) continue;
                        const key = signature(parent) + ' > ' + signature(el);
                        if (!groups.has(key)) groups.set(key, []);
                        groups.get(key).push(el);
                    }

                    return Array.from(groups.entries())
                        .filter(([k, els]) => els.length >= 3)
                        .sort((a, b) => b[1].length - a[1].length)
                        .slice(0, topN)
                        .map(([key, els]) => ({
                            signature: key,
                            count: els.length,
                            samples: els.slice(0, 3).map(el => ({
                                tag: el.tagName,
                                role: el.getAttribute('role'),
                                testid: el.getAttribute('data-testid'),
                                href: el.getAttribute('href'),
                                text: cleanText(el),
                                outerHTMLSnippet: el.outerHTML.slice(0, 200),
                            })),
                        }));
                }
            """, top_n)
        except PlaywrightError as e:
            logger.debug("Structure dump failed: %s", e)
            return

        logger.debug("Top repeated element structures on the page (tag-agnostic):")
        for g in groups or []:
            logger.debug("  %s  (repeats %dx)", g['signature'], g['count'])
            for s in g["samples"]:
                logger.debug(
                    "      tag=%s role=%r testid=%r href=%r text=%r",
                    s['tag'], s['role'], s['testid'], s['href'], s['text'],
                )
                logger.debug("      html: %s", s['outerHTMLSnippet'])

    def _handle_search_state(self) -> None:
        logger.info("[SEARCH] Navigating to job board and scraping listings...")
        context = self._new_context()
        page = context.new_page()

        try:
            page.goto(self.search_url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass  # best-effort settle; proceed with whatever rendered
        except PlaywrightError as e:
            self._last_error = f"Failed to load search page: {e}"
            self.current_state = ApplicationState.ERROR
            return

        self._wait_for_page_to_settle(page)
        self._dump_repeated_structures(page)

        try:
            listings = self._scrape_job_cards(page)
        except PlaywrightError as e:
            self._last_error = f"Failed to scrape job cards: {e}"
            self.current_state = ApplicationState.ERROR
            return
        finally:
            context.close()
            self._current_context = None

        if not listings:
            logger.warning("[SEARCH] No job listings found on this page.")
            self.current_state = ApplicationState.DONE
            return

        logger.info(f"[SEARCH] Scraped {len(listings)} listing(s). Filtering against criteria via LLM...")
        filtered = self._filter_listings(listings)
        logger.info(f"[SEARCH] {len(filtered)}/{len(listings)} listing(s) matched criteria.")

        new_urls = [listing.url for listing in filtered if listing.url not in self.state.processed]

        if self.resume_from_url and self.resume_from_url in new_urls:
            idx = new_urls.index(self.resume_from_url)
            logger.info(f"[SEARCH] Resuming after {self.resume_from_url} -- skipping {idx + 1} already-seen entr(y/ies).")
            new_urls = new_urls[idx + 1:]

        self.state.queue = new_urls
        self.state.save(self.state_path)

        if not self.state.queue:
            logger.info("[SEARCH] Nothing new to process. Done.")
            self.current_state = ApplicationState.DONE
            return

        self.current_state = ApplicationState.NAVIGATION

    def _scrape_job_cards(self, page: Page) -> list[JobListing]:
        """
        Best-effort, site-agnostic job card scraper: tries several common
        anchor/container patterns rather than hardcoding one platform's
        selectors, then picks whichever selector returned the most
        visible, non-sidebar results (a real job list is virtually always
        larger than a nav menu). Filters out elements that aren't
        actually visible on screen (zero size, display:none), since a
        collapsed dropdown menu can be present in the DOM without ever
        rendering.
        """
        try:
            result = page.evaluate(r"""
                (selectors) => {
                    function cleanText(el) {
                        // Some accessible SPA components (e.g. Handshake job
                        // cards) put the full descriptive text only in
                        // aria-label, with empty visible text otherwise.
                        const ariaLabel = (el.getAttribute('aria-label') || '').trim();
                        if (ariaLabel) return ariaLabel.replace(/\s+/g, ' ');
                        return (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
                    }
                    function isSidebar(el) {
                        return !!el.closest(
                            'aside, [role="navigation"], ' +
                            '[class*="sidebar" i], [id*="sidebar" i], ' +
                            '[class*="side-nav" i], [class*="sidenav" i]'
                        );
                    }
                    function isVisible(el) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        return true;
                    }

                    const perSelector = [];
                    for (const sel of selectors) {
                        let anchors;
                        try {
                            anchors = Array.from(document.querySelectorAll(sel));
                        } catch (e) {
                            perSelector.push({ selector: sel, error: String(e) });
                            continue;
                        }
                        const visible = anchors.filter(isVisible);
                        const filtered = visible.filter(a => !isSidebar(a));
                        const finalSet = filtered.length > 0 ? filtered : visible;

                        perSelector.push({
                            selector: sel,
                            raw_count: anchors.length,
                            visible_count: visible.length,
                            after_sidebar_filter_count: filtered.length,
                            items: finalSet.map(a => ({ href: a.getAttribute('href'), text: cleanText(a) })),
                        });
                    }
                    return perSelector;
                }
            """, [
                'a[data-testid*="job" i]',
                'a[class*="job-title" i]',
                'article a',
                # CSS-in-JS class names carry no semantic meaning and can
                # change on any deploy -- role/href are the stable signal.
                'a[role="button"][href]',
                'li a',
            ])
        except PlaywrightError as e:
            logger.warning(f"[SEARCH] Job card scrape failed: {e}")
            return []

        logger.debug("Job card selector breakdown:")
        for entry in result or []:
            if "error" in entry:
                logger.debug("  %r: ERROR — %s", entry['selector'], entry['error'])
                continue
            logger.debug(
                "  %r: raw=%d visible=%d after_sidebar_filter=%d",
                entry['selector'], entry['raw_count'], entry['visible_count'],
                entry['after_sidebar_filter_count'],
            )
            for item in entry["items"][:5]:
                logger.debug("      sample: %r -> %r", item['text'], item['href'])

        best_entry = None
        for entry in result or []:
            if "error" in entry:
                continue
            if best_entry is None or len(entry["items"]) > len(best_entry["items"]):
                best_entry = entry

        if not best_entry or not best_entry["items"]:
            logger.debug("No selector found any visible, non-sidebar candidates.")
            return []

        logger.debug("Selected %r (%d item(s)).", best_entry['selector'], len(best_entry['items']))

        seen: set[str] = set()
        listings: list[JobListing] = []
        for entry in best_entry["items"]:
            href = entry.get("href")
            text = (entry.get("text") or "").strip()
            if not href or not text or len(text) < 3:
                continue
            url = urljoin(page.url, href)
            if url in seen:
                continue
            seen.add(url)
            listings.append(JobListing(url=url, title=text))

        return listings

    def _filter_listings(self, listings: list[JobListing]) -> list[JobListing]:
        """
        Reuses aiMatcher.py's local Ollama call to relevance-filter job
        titles against a plain-language criteria string. Same rule as
        form-field matching applies: the output key (listing_id) must be
        copied verbatim, never invented.
        """
        if self.model:
            aiMatcher_module.MODEL_NAME = self.model

        listing_payload = [
            {"listing_id": f"listing_{i}", "title": l.title, "url": l.url}
            for i, l in enumerate(listings)
        ]
        system_prompt = (
            "You are a job-listing relevance filter. Given a list of "
            "scraped items and a plain-language criteria string, decide "
            "which items are BOTH (a) genuinely job postings and (b) "
            "match the criteria. Respond with ONLY valid JSON: a flat "
            "object where each key is the listing's exact 'listing_id' "
            "string, copied character-for-character from the input -- "
            "never invent, rename, or reformat it. Every listing_id from "
            "the input MUST appear as a key in your output. Each value "
            'must be an object with exactly this shape: '
            '{"matches_criteria": true or false, "reason": "<short '
            'reason, under 15 words>"}.\n\n'
            "IMPORTANT: some scraped items are NOT real job postings -- "
            "they're site navigation links, feature promos, or platform "
            "pages that got swept up by the scraper (e.g. \"AI Showcase\", "
            "\"Saved Jobs\", \"Messages\", \"Fellow Program Home\", "
            "\"Settings\"). A real job title virtually always names a "
            "role, and often a department/seniority/location (e.g. "
            "\"Software Engineering Intern\", \"Senior Product Manager - "
            "Remote\"). If a title reads like a navigation menu item, "
            "platform feature, or generic page name rather than an "
            "actual job role, set matches_criteria to false regardless "
            "of whether it sounds topically relevant -- explain this in "
            "the reason (e.g. \"not a real job title, appears to be site "
            "navigation\")."
        )
        user_prompt = (
            f"{json.dumps({'criteria': self.criteria, 'listings': listing_payload}, indent=2)}\n\n"
            "Return ONLY the JSON object now. No prose, no markdown fences."
        )

        logger.debug("Criteria sent to LLM: %s", self.criteria)
        logger.debug("%d listing(s) sent to LLM:", len(listing_payload))
        for item in listing_payload:
            logger.debug("  %s: %r", item['listing_id'], item['title'])

        try:
            raw = aiMatcher_module._call_ollama(system_prompt, user_prompt)
            parsed = aiMatcher_module._parse_llm_json(raw)
        except Exception as e:
            logger.warning(f"[SEARCH] LLM filtering failed ({e}); keeping all listings unfiltered.")
            return listings

        logger.debug("Raw LLM response:\n%s", raw)
        logger.debug("Per-listing decisions (%d total):", len(listings))
        for i, listing in enumerate(listings):
            entry = parsed.get(f"listing_{i}")
            if isinstance(entry, dict):
                decision = "KEEP" if entry.get("matches_criteria") else "reject"
                reason = entry.get("reason", "<no reason given>")
            else:
                decision = "reject"
                reason = "<no entry returned by LLM for this listing_id>"
            logger.debug("  [%-6s] %r — %s", decision, listing.title, reason)

        kept = []
        for i, listing in enumerate(listings):
            entry = parsed.get(f"listing_{i}")
            if isinstance(entry, dict) and entry.get("matches_criteria"):
                kept.append(listing)
        return kept

    # ------------------------------------------------------------------ #
    # NAVIGATION_STATE
    # ------------------------------------------------------------------ #
    def _handle_navigation_state(self) -> None:
        if not self.state.queue:
            logger.info("[NAVIGATION] Queue empty. Re-running search for more listings.")
            self.current_state = ApplicationState.SEARCH
            return

        job_url = self.state.queue[0]
        logger.info(f"[NAVIGATION] Navigating to: {job_url}")
        self._polite_delay()

        context = self._new_context()
        page = context.new_page()
        try:
            page.goto(job_url, wait_until="domcontentloaded")
        except PlaywrightError as e:
            self._last_error = f"Failed to load job page {job_url}: {e}"
            self.current_state = ApplicationState.ERROR
            return

        self._current_page = page
        apply_type, apply_locator = self._classify_apply_button(page)

        if apply_type == ApplyType.EXTERNAL:
            logger.info(f"[NAVIGATION] {job_url} is an EXTERNAL apply -- logging and skipping.")
            log_application(job_url, status="EXTERNAL", notes="Off-site apply flow; skipped")

            if self.open_external_in_new_tab and apply_locator:
                try:
                    with context.expect_page() as new_page_info:
                        apply_locator.click()
                    new_tab = new_page_info.value
                    logger.info(f"[NAVIGATION] Opened external apply in a new tab for manual handling: {new_tab.url}")
                except PlaywrightError as e:
                    logger.warning(f"[NAVIGATION] Could not open external apply in a new tab: {e}")

            self._mark_processed(job_url)
            self.current_state = ApplicationState.NAVIGATION
            return

        if apply_type == ApplyType.UNKNOWN:
            self._last_error = f"Could not find an Apply/Easy Apply button on {job_url}"
            self.current_state = ApplicationState.ERROR
            return

        # EASY_APPLY
        try:
            apply_locator.click()
            self._polite_delay()
        except PlaywrightError as e:
            self._last_error = f"Failed to click Easy Apply on {job_url}: {e}"
            self.current_state = ApplicationState.ERROR
            return

        self.current_state = ApplicationState.INTERACTION

    def _find_apply_button(self, page: Page, timeout_ms: int = 6000) -> Locator | None:
        """
        Finds the job's Apply/Quick-Apply trigger via visible text
        matching apply-related phrasing only (button role, then link
        role, then any text match) -- never falls back to a bare
        [role="button"]/[type="submit"] selector, since a job detail page
        has many unrelated role="button" elements (filters, bookmarks,
        share menus) that such a blind match could grab instead.
        """
        for role in ("button", "link"):
            try:
                candidate = page.get_by_role(role, name=APPLY_BUTTON_TEXT_PATTERN).first
                candidate.wait_for(state="visible", timeout=timeout_ms)
                if candidate.is_enabled():
                    return candidate
            except (PlaywrightTimeoutError, PlaywrightError):
                continue

        try:
            candidate = page.get_by_text(APPLY_BUTTON_TEXT_PATTERN).first
            candidate.wait_for(state="visible", timeout=timeout_ms)
            return candidate
        except (PlaywrightTimeoutError, PlaywrightError):
            pass

        return None

    def _classify_apply_button(self, page: Page) -> tuple[ApplyType, Locator | None]:
        """Classifies EASY_APPLY vs EXTERNAL by checking the Apply button's
        text and, if it's a link, whether its href leaves the current origin."""
        try:
            apply_locator = self._find_apply_button(page, timeout_ms=6000)
        except Exception:
            apply_locator = None

        if not apply_locator:
            return ApplyType.UNKNOWN, None

        try:
            text = apply_locator.inner_text(timeout=500).lower()
            href = apply_locator.get_attribute("href") or ""
        except PlaywrightError:
            text, href = "", ""

        if any(kw in text for kw in EXTERNAL_APPLY_KEYWORDS):
            return ApplyType.EXTERNAL, apply_locator

        current_origin = page.url.split("/")[2] if "//" in page.url else ""
        if href.startswith("http") and current_origin and current_origin not in href:
            return ApplyType.EXTERNAL, apply_locator

        return ApplyType.EASY_APPLY, apply_locator

    # ------------------------------------------------------------------ #
    # INTERACTION_STATE -- existing pipeline plugs in here
    # ------------------------------------------------------------------ #
    def _handle_interaction_state(self) -> None:
        page = self._current_page
        job_url = self.state.queue[0]

        for page_num in range(1, MAX_PAGES_PER_APPLICATION + 1):
            logger.info(f"[INTERACTION] Application page {page_num} for {job_url}")

            try:
                fields = run_scrape_stage(page)
                match_data = run_match_stage(fields, self.config_path, self.model)
                injection_report = run_injection_stage(page, match_data)
            except (ScrapeError, MatchError, InjectionError) as e:
                self._last_error = f"Pipeline failed on page {page_num} of {job_url}: {e}"
                self.current_state = ApplicationState.ERROR
                return

            next_locator = self._find_next_or_submit(page)
            if next_locator is None:
                self._last_error = f"No Next/Continue/Submit button found on page {page_num} of {job_url}"
                self.current_state = ApplicationState.ERROR
                return

            button_text = self._safe_text(next_locator).lower()
            is_final_submit = any(kw in button_text for kw in SUBMIT_LIKE_KEYWORDS)

            if is_final_submit:
                # The one step that never gets automated away, regardless
                # of how many Next-pages preceded it.
                logger.info(f"[INTERACTION] Reached final submit for {job_url} -- opening review gate.")
                try:
                    run_review_stage(page, injection_report)
                except ReviewError as e:
                    self._last_error = f"Review gate failed for {job_url}: {e}"
                    self.current_state = ApplicationState.ERROR
                    return

                self.applications_submitted += 1
                self._mark_processed(job_url)
                self.current_state = ApplicationState.NAVIGATION
                return

            try:
                next_locator.click()
                self._polite_delay()
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            except PlaywrightError as e:
                self._last_error = f"Failed to advance past page {page_num} of {job_url}: {e}"
                self.current_state = ApplicationState.ERROR
                return

        self._last_error = f"Exceeded {MAX_PAGES_PER_APPLICATION} pages without reaching Submit on {job_url}"
        self.current_state = ApplicationState.ERROR

    def _find_next_or_submit(self, page: Page) -> Locator | None:
        for selector in NEXT_BUTTON_PRIORITY:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=2000)
                if locator.is_enabled():
                    return locator
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
        # Fall back to review.py's submit-button search -- a final page's
        # button is very often literally "Submit"/"Submit Application".
        try:
            return find_and_wait_for_submit(page, timeout_ms=3000)
        except Exception:
            return None

    @staticmethod
    def _safe_text(locator: Locator) -> str:
        try:
            return locator.inner_text(timeout=300).strip()
        except PlaywrightError:
            return ""

    # ------------------------------------------------------------------ #
    # ERROR_STATE
    # ------------------------------------------------------------------ #
    def _handle_error_state(self) -> None:
        job_url = self.state.queue[0] if self.state.queue else self.state.search_url
        logger.error(f"[ERROR] {self._last_error}")
        log_application(job_url, status="ERROR", notes=self._last_error or "Unknown error")

        if self._current_context:
            try:
                self._current_context.close()
            except PlaywrightError:
                pass
            self._current_context = None
        self._current_page = None

        # Don't retry the same job indefinitely -- mark it processed (as
        # failed) and move on.
        if self.state.queue:
            self._mark_processed(job_url)

        self._last_error = None
        self.current_state = ApplicationState.NAVIGATION if self.state.queue else ApplicationState.SEARCH

    def _mark_processed(self, job_url: str) -> None:
        if job_url in self.state.queue:
            self.state.queue.remove(job_url)
        if job_url not in self.state.processed:
            self.state.processed.append(job_url)
        self.state.last_processed_url = job_url
        self.state.save(self.state_path)


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agentic multi-job application navigator.")
    parser.add_argument("--search-url", required=True, help="Job board search results URL.")
    parser.add_argument(
        "--criteria",
        default=None,
        help=(
            "Plain-language criteria for the LLM filter. If omitted, derived "
            "automatically from config.json's application_preferences section "
            "(job_titles_of_interest, industries_of_interest, remote_preference, "
            "excluded_companies)."
        ),
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json.")
    parser.add_argument("--model", default=None, help="Ollama model override.")
    parser.add_argument("--max-applications", type=int, default=None, help="Stop after N submissions.")
    parser.add_argument("--headless", action="store_true", help="Run headless (disables the review gate pause!).")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Enable DEBUG-level logging, including full scraped-candidate "
            "payloads, raw LLM responses, and selector diagnostics. Off by "
            "default since this can print personal data (your config.json "
            "profile) to the console/log file."
        ),
    )
    parser.add_argument("--state-file", default="navigator_state.json", help="Resumable state file path.")
    parser.add_argument("--resume-from", default=None, help="Skip forward past this job URL on the next search.")
    parser.add_argument("--open-external-in-new-tab", action="store_true")
    parser.add_argument(
        "--auth-state",
        default=None,
        help=(
            "Path to a saved login session (from login.py), for sites "
            "like Handshake that require authentication before search results "
            "render. Omit for sites that don't need login."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.verbose:
        # aiMatcher / jobScraper / injection / review / job_navigator all
        # log through their own named loggers with no explicit level set,
        # so raising the root logger's level is enough to let their
        # logger.debug() calls (payload dumps, raw LLM responses, selector
        # diagnostics) reach the console -- left at INFO by default since
        # those dumps can include personal data from config.json.
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        navigator = JobNavigator(
            search_url=args.search_url,
            criteria=args.criteria,
            config_path=args.config,
            model=args.model,
            max_applications=args.max_applications,
            headless=args.headless,
            state_file=args.state_file,
            resume_from_url=args.resume_from,
            open_external_in_new_tab=args.open_external_in_new_tab,
            auth_state_path=args.auth_state,
        )
    except ValueError as e:
        print(f"[FATAL] {e}")
        sys.exit(1)
    navigator.run()