#!/usr/bin/env python3
"""
Pipeline:
    scrape -> match -> inject -> review

main.py owns the entire Playwright browser lifecycle: it launches the
browser and creates the page exactly once, passes that same page through
every stage, and is the only file that ever calls browser.close(). None of
jobScraper.py / injection.py / review.py launch or close a browser.

Module contracts this file expects:

    jobScraper.JobFormScraper.scrape_page(page) -> dict
        Scrapes an already-navigated page (and its frames).
        Returns: {"source_url": ..., "field_count": ..., "fields": [...]}

    aiMatcher.match_fields(form_fields: list[dict], config: dict) -> MatchResult
        Pure data + HTTP call — no page/browser involved.

    injection.inject_matched_fields(page, matched_fields: list[dict]) -> InjectionReport
        Fills the given page and applies confidence-based CSS borders.

    review.run_review_and_submit(page) -> None
        Opens the pause, prompts to submit, clicks Submit, logs the
        outcome to applications_log.csv — all in one call.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

try:
    import aiMatcher as aiMatcher_module
    from aiMatcher import match_fields, OllamaConnectionError, OllamaResponseError
    from injection import inject_matched_fields, InjectionReport
    from jobScraper import JobFormScraper
    from review import run_review_and_submit
except ImportError as e:
    print(
        f"[FATAL] Could not import a required module: {e}\n"
        "Make sure jobScraper.py, aiMatcher.py, injection.py, and review.py "
        "are in the same directory as main.py (or on PYTHONPATH)."
    )
    sys.exit(1)

# ---------- Configuration defaults ----------
DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_NAV_TIMEOUT_MS = 30_000
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


# ---------- Logging setup ----------
def setup_logging(verbose: bool = False) -> logging.Logger:
    """
    The file log always captures DEBUG-level detail for this run. The
    console only shows DEBUG-level messages (including the full scraped
    payloads and raw LLM responses aiMatcher.py/jobScraper.py log at
    debug level) when verbose=True, since those can include personal
    data from config.json.
    """
    logger = logging.getLogger("job_agent")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    # aiMatcher.py (imported above) installs a handler on the root logger
    # via its own logging.basicConfig() call. Without this, every message
    # logged here would also propagate up and print a second time through
    # that root handler.
    logger.propagate = False

    log_file = LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    file_handler.setFormatter(fmt)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def configure_verbosity(verbose: bool) -> None:
    """
    Raises the console log level for main.py's own "job_agent" logger,
    and raises the root logger's level so aiMatcher / jobScraper /
    injection / review's own loggers (which set no explicit level and
    propagate to root) also start emitting their logger.debug() calls --
    full scraped-candidate payloads, raw LLM responses, selector
    diagnostics. Off by default: those calls include personal data from
    config.json and shouldn't print unconditionally.
    """
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger().setLevel(logging.DEBUG if verbose else logging.INFO)


logger = setup_logging()


# ---------- Custom exceptions, one per pipeline stage ----------
class ScrapeError(Exception):
    pass


class MatchError(Exception):
    pass


class InjectionError(Exception):
    pass


class ReviewError(Exception):
    pass


# ---------- Pipeline stage wrappers ----------
def run_scrape_stage(page: Page) -> list[dict]:
    """Scrapes form fields from an already-navigated page."""
    logger.info("Stage 1/4: Scraping form fields...")
    scraper = JobFormScraper(nav_timeout_ms=DEFAULT_NAV_TIMEOUT_MS)
    try:
        bundle = scraper.scrape_page(page)
    except Exception as e:
        raise ScrapeError(f"Unexpected scraper failure: {e}") from e

    fields = bundle.get("fields", [])
    if not fields:
        raise ScrapeError(
            "Scraper returned zero fields — page may not have loaded, "
            "or the form is behind a login/CAPTCHA."
        )
    logger.info(f"Scraped {len(fields)} form field(s).")
    return fields


def _to_matcher_fields(scraped_fields: list[dict]) -> list[dict]:
    """
    Translates jobScraper's field shape into what aiMatcher.match_fields
    expects. field_id is the scraper's human-readable context_key (e.g.
    "graduation_date", "visa_sponsorship"), not a raw CSS selector — the
    LLM reasons about this string directly. The real Playwright selector
    (selector_hint) rides along separately and gets re-attached in
    _to_injection_records.
    """
    out = []
    for i, f in enumerate(scraped_fields):
        out.append({
            "field_id": f.get("context_key") or f.get("selector_hint") or f.get("element_id") or f"field_{i}",
            "label": f.get("label") or f.get("placeholder") or f.get("aria_label"),
            "type": f.get("field_type", "text"),
            "options": f.get("options", []),
            "selector_hint": f.get("selector_hint"),
            "option_selectors": f.get("option_selectors", {}),
            "placeholder": f.get("placeholder"),
            "name": f.get("name"),
        })
    return out


def run_match_stage(scraped_fields: list[dict], config_path: str, model: str | None = None) -> dict:
    """Loads config.json and sends scraped fields to the local Ollama matcher."""
    logger.info("Stage 2/4: Matching fields via local Ollama model...")
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError as e:
        raise MatchError(f"Config file not found at '{config_path}': {e}") from e
    except json.JSONDecodeError as e:
        raise MatchError(f"Config file at '{config_path}' is not valid JSON: {e}") from e

    if model:
        # aiMatcher.py hardcodes MODEL_NAME as a module constant; override
        # it directly if the caller requested a specific model.
        aiMatcher_module.MODEL_NAME = model

    matcher_fields = _to_matcher_fields(scraped_fields)

    try:
        result = match_fields(matcher_fields, config)
    except (OllamaConnectionError, OllamaResponseError) as e:
        raise MatchError(str(e)) from e
    except ValueError as e:
        raise MatchError(f"Malformed field data sent to matcher: {e}") from e
    except Exception as e:
        raise MatchError(f"AI matching failed: {e}") from e

    if not result.matches:
        raise MatchError("AI matcher returned no mapped answers.")

    flagged = len(result.review_queue())
    logger.info(f"Matched {len(result.matches)} field(s); {flagged} flagged for review.")

    lookup = {f["field_id"]: f for f in matcher_fields}
    return {"matches": result.matches, "field_lookup": lookup}


def _to_injection_records(match_data: dict) -> list[dict]:
    """
    Reshapes aiMatcher's field_id-keyed output (confidence 0-100) into the
    selector/field_type/value/confidence(0-1)/label shape injection.py
    expects. Low-confidence and null-value fields are kept (not dropped)
    so injection.py highlights them red for manual review.

    "selector" comes from selector_hint (the real locator) — field_id is
    now a human-readable string and would match nothing as a CSS
    selector. placeholder/name ride along too; injection.py's
    _resolve_locator() uses them alongside label as a resolution cascade,
    since selector_hint alone can go stale between scrape and injection
    on frameworks (e.g. Radix UI) that regenerate element ids on re-render.
    """
    records = []
    for field_id, match in match_data["matches"].items():
        source = match_data["field_lookup"].get(field_id, {})
        records.append({
            "selector": source.get("selector_hint"),
            "field_type": source.get("type", "text"),
            "value": match.value,
            "confidence": (match.confidence or 0) / 100.0,
            "label": source.get("label") or field_id,
            "option_selectors": source.get("option_selectors", {}),
            "placeholder": source.get("placeholder"),
            "name": source.get("name"),
        })
    return records


def run_injection_stage(page: Page, match_data: dict) -> InjectionReport:
    logger.info("Stage 3/4: Injecting answers into the page...")
    records = _to_injection_records(match_data)
    try:
        report = inject_matched_fields(page, records)
    except PlaywrightTimeoutError as e:
        raise InjectionError(f"Timed out while filling a field: {e}") from e
    except Exception as e:
        raise InjectionError(f"Unexpected injection failure: {e}") from e

    logger.info(
        f"Injected fields — {report.high_confidence_count} high-confidence, "
        f"{report.needs_review_count} flagged for review."
    )
    return report


def run_review_stage(page: Page, injection_report: InjectionReport) -> None:
    """review.run_review_and_submit() owns the pause, CLI confirm, submit click, and CSV log entry."""
    logger.info("Stage 4/4: Handing off to human review gate...")
    injection_report.print_summary()
    try:
        run_review_and_submit(page)
    except Exception as e:
        raise ReviewError(f"Review gate failed: {e}") from e


# ---------- Orchestrator ----------
def main(
    target_url: str,
    config_path: str = DEFAULT_CONFIG_PATH,
    model: str | None = None,
    headless: bool = False,
    verbose: bool = False,
) -> None:
    """
    Runs the full scrape -> match -> inject -> review pipeline against a
    single job application URL, using one browser/page for the whole run.
    Each stage is isolated so a failure at any point stops the run without
    submitting anything.
    """
    configure_verbosity(verbose)

    if headless:
        logger.warning(
            "Running headless — review.py's page.pause() will not work; "
            "the review gate will stall waiting for a resume that can't happen."
        )

    logger.info(f"=== Starting job agent run for: {target_url} ===")

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(DEFAULT_NAV_TIMEOUT_MS)

            try:
                page.goto(target_url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as e:
                logger.error(f"Failed to load page: {e}")
                return

            try:
                # Best-effort settle for SPA-heavy pages; not fatal if it never truly idles.
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass

            try:
                fields = run_scrape_stage(page)
            except ScrapeError as e:
                logger.error(f"Aborting run — scrape stage failed: {e}")
                return

            try:
                match_data = run_match_stage(fields, config_path, model)
            except MatchError as e:
                logger.error(f"Aborting run — AI matching stage failed: {e}")
                return

            try:
                injection_report = run_injection_stage(page, match_data)
            except InjectionError as e:
                logger.error(f"Aborting run — injection stage failed: {e}")
                return

            try:
                run_review_stage(page, injection_report)
            except ReviewError as e:
                logger.error(f"Aborting run — review stage failed: {e}")
                return

            logger.info("Review/submission stage complete — see applications_log.csv.")

        except Exception as e:
            logger.exception(f"Unhandled error in pipeline: {e}")
        finally:
            if browser:
                browser.close()
            logger.info("=== Run finished. Browser closed. ===\n")


# ---------- CLI entry point ----------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local job application agent orchestrator.")
    parser.add_argument("--url", required=True, help="Job application URL to process.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.json.")
    parser.add_argument(
        "--model",
        default=None,
        help="Ollama model name (overrides aiMatcher.py's default MODEL_NAME if set).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (disables the visual page.pause() review step).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Enable DEBUG-level logging, including full scraped-candidate "
            "payloads, raw LLM responses, and selector diagnostics. Off by "
            "default since this can print personal data (your config.json "
            "profile) to the console."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(
        target_url=args.url,
        config_path=args.config,
        model=args.model,
        headless=args.headless,
        verbose=args.verbose,
    )