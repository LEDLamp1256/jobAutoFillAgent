"""
job_navigator.py — Agentic multi-job orchestrator.

Drives SEARCH -> NAVIGATION -> INTERACTION -> ERROR across a list of job
postings, reusing the existing scrape -> match -> inject -> review pipeline
(main.py / aiMatcher.py / injection.py / review.py) for each individual
application rather than reimplementing any of that logic.

TWO DELIBERATE DEVIATIONS FROM A LITERAL "FULLY AUTONOMOUS" SPEC:

1. SUBMISSION ALWAYS GOES THROUGH THE HUMAN REVIEW GATE.
   INTERACTION_STATE automates navigation BETWEEN pages of a single
   multi-step application form (clicking "Next"/"Continue"), but the
   final Submit always routes through review.run_review_and_submit() --
   the page.pause() + CLI confirmation + verified-submission gate built
   earlier in this project. This never gets automated away, regardless
   of how many Next-pages preceded it. Auto-submitting your real personal
   data to many different real employers with zero per-application
   review is a materially bigger risk than a single reviewed form.

2. NO BOT-DETECTION EVASION. There's no "mimic human behavior" delay
   logic and no context-rotation framed around avoiding tracking.
   LinkedIn and Indeed's Terms of Service prohibit automated scraping and
   application bots -- building tooling whose explicit purpose is
   evading their detection isn't implemented here. What IS here instead:
     - A fresh browser context per application, for session hygiene
       (avoiding stale cookies/state bleeding between unrelated
       applications) -- good practice on its own merits.
     - Modest, fixed-range delays between actions, to avoid hammering
       the target site's infrastructure -- not human impersonation.
   This does NOT make automated use of LinkedIn/Indeed compliant with
   their ToS. Read the target site's terms before pointing this at a
   real account. Greenhouse/Lever/Ashby/Workday-hosted career pages
   generally don't prohibit this the way LinkedIn/Indeed do.

INTEGRATION WITH YOUR EXISTING PIPELINE:
INTERACTION_STATE calls main.py's run_scrape_stage(page),
run_match_stage(fields, config_path, model), run_injection_stage(page,
match_data), and run_review_stage(page, injection_report) directly --
the exact same functions main.py's single-form pipeline already uses.
Nothing about scraping, matching, or injecting is reimplemented here.
"""

from __future__ import annotations

import argparse
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

# Reuse main.py's existing pipeline stages + exceptions rather than
# reimplementing scrape/match/inject/review.
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


# Buttons that advance a multi-page application WITHOUT submitting it.
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
    already processed. `processed` includes jobs that errored out too --
    per the ERROR_STATE spec, a failed job is skipped, not retried forever.
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
        criteria: str,
        config_path: str = "config.json",
        model: str | None = None,
        max_applications: int | None = None,
        headless: bool = False,
        state_file: str = "navigator_state.json",
        min_delay_s: float = 2.0,
        max_delay_s: float = 5.0,
        open_external_in_new_tab: bool = False,
        resume_from_url: str | None = None,
    ):
        self.search_url = search_url
        self.criteria = criteria
        self.config_path = config_path
        self.model = model
        self.max_applications = max_applications
        self.headless = headless
        self.state_path = Path(state_file)
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s
        self.open_external_in_new_tab = open_external_in_new_tab
        self.resume_from_url = resume_from_url

        self.state: NavigatorState = NavigatorState.load(self.state_path) or NavigatorState(search_url=search_url)
        self.applications_submitted = 0
        self.current_state = ApplicationState.SEARCH

        self._playwright = None
        self._browser: Browser | None = None
        self._current_context: BrowserContext | None = None
        self._current_page: Page | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ #
    # Politeness delay -- see module docstring: NOT bot-detection evasion
    # ------------------------------------------------------------------ #
    def _polite_delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))

    # ------------------------------------------------------------------ #
    # Browser/context lifecycle -- fresh context per application
    # ------------------------------------------------------------------ #
    def _new_context(self) -> BrowserContext:
        """A fresh context per application for session hygiene: keeps one
        job's cookies/login state from leaking into the next, and stops a
        crash on job N from corrupting state used for job N+1."""
        if self._current_context:
            try:
                self._current_context.close()
            except PlaywrightError:
                pass
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
        Best-effort, site-agnostic job card scraper. Job boards vary
        widely in markup -- this tries a few common patterns (an anchor
        with substantial text inside a card/list container) rather than
        hardcoding selectors for any one platform. Expect to tune
        `selectors` below for whatever board you actually point this at.
        """
        selectors = [
            'a[data-testid*="job" i]',
            'a[class*="job-title" i]',
            'article a',
            'li a',
        ]
        seen: set[str] = set()
        listings: list[JobListing] = []

        for selector in selectors:
            try:
                anchors = page.locator(selector).all()
            except PlaywrightError:
                continue
            for a in anchors:
                try:
                    href = a.get_attribute("href")
                    text = a.inner_text(timeout=200).strip()
                except PlaywrightError:
                    continue
                if not href or not text or len(text) < 3:
                    continue
                url = urljoin(page.url, href)
                if url in seen:
                    continue
                seen.add(url)
                listings.append(JobListing(url=url, title=text))
            if listings:
                break  # first selector that found anything wins

        return listings

    def _filter_listings(self, listings: list[JobListing]) -> list[JobListing]:
        """
        Reuses aiMatcher.py's local Ollama call to relevance-filter job
        titles against a plain-language criteria string, applying the
        same lesson learned from form-field matching: the output key
        (listing_id) must be copied verbatim, never invented.
        """
        if self.model:
            aiMatcher_module.MODEL_NAME = self.model

        listing_payload = [
            {"listing_id": f"listing_{i}", "title": l.title, "url": l.url}
            for i, l in enumerate(listings)
        ]
        system_prompt = (
            "You are a job-listing relevance filter. Given a list of "
            "scraped job postings and a plain-language criteria string, "
            "decide which postings match. Respond with ONLY valid JSON: a "
            "flat object where each key is the listing's exact "
            "'listing_id' string, copied character-for-character from the "
            "input -- never invent, rename, or reformat it. Every "
            "listing_id from the input MUST appear as a key in your "
            "output. Each value must be an object with exactly this "
            'shape: {"matches_criteria": true or false, "reason": '
            '"<short reason, under 15 words>"}.'
        )
        user_prompt = (
            f"{json.dumps({'criteria': self.criteria, 'listings': listing_payload}, indent=2)}\n\n"
            "Return ONLY the JSON object now. No prose, no markdown fences."
        )

        try:
            raw = aiMatcher_module._call_ollama(system_prompt, user_prompt)
            parsed = aiMatcher_module._parse_llm_json(raw)
        except Exception as e:
            logger.warning(f"[SEARCH] LLM filtering failed ({e}); keeping all listings unfiltered.")
            return listings

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

    def _classify_apply_button(self, page: Page) -> tuple[ApplyType, Locator | None]:
        """
        Reuses review.py's fuzzy button finder (its "Apply"/"Submit"
        phrasing overlaps what job pages use for their apply CTA), then
        classifies EASY_APPLY vs EXTERNAL by checking the button's text
        and, if it's a link, whether its href leaves the current origin.
        """
        try:
            apply_locator = find_and_wait_for_submit(page, timeout_ms=6000)
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
    # INTERACTION_STATE -- your existing pipeline plugs in here
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
                # This is the one step that never gets automated away,
                # regardless of how many Next-pages preceded it -- same
                # review.py gate used by the single-form pipeline.
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

            # Not the final page yet -- advance and loop.
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
        # Fall back to review.py's own submit-button search -- a final
        # page's button is very often literally "Submit"/"Submit Application".
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
        # failed) and move on, per the spec.
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
    parser.add_argument("--criteria", required=True, help="Plain-language criteria for the LLM filter.")
    parser.add_argument("--config", default="config.json", help="Path to config.json.")
    parser.add_argument("--model", default=None, help="Ollama model override.")
    parser.add_argument("--max-applications", type=int, default=None, help="Stop after N submissions.")
    parser.add_argument("--headless", action="store_true", help="Run headless (disables the review gate pause!).")
    parser.add_argument("--state-file", default="navigator_state.json", help="Resumable state file path.")
    parser.add_argument("--resume-from", default=None, help="Skip forward past this job URL on the next search.")
    parser.add_argument("--open-external-in-new-tab", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
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
    )
    navigator.run()