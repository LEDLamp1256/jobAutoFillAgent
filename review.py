"""
Assumes extraction, AI field matching, injection, confidence-based styling. 
This module is ATS-agnostic by design -- it never assumes a single fixed 
selector, page structure, or frame layout, since every ATS platform builds 
its "Submit" step differently. Instead it:

  - Tries a priority-ordered list of selectors for the submit button,
    across the main frame and every iframe.
  - Validates that Phase 4's injected fields actually look resolved
    before handing control to the human, and prints what still needs
    attention.
  - Hands control to the human via page.pause() and detects a manual
    Submit click during that pause, so the CSV log is accurate either way.
  - Never crashes on a timeout — falls back to a page analysis and still
    pauses for manual intervention.

Nothing in this file submits anything without either explicit "Y"
confirmation at the CLI, or a manual click in the browser during the
pause.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.sync_api import (
    Frame,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)

# Reuse injection.py's confidence threshold so "red-bordered" means the
# same thing here as when Phase 4 drew the border.
from injection import CONFIDENCE_THRESHOLD

if TYPE_CHECKING:
    from injection import InjectionReport

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("review_gate")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

LOG_FILE = Path("applications_log.csv")
LOG_FIELDS = ["timestamp_utc", "url", "status", "notes"]

# Priority-ordered: broader/riskier selectors (e.g. [role="button"]) sit
# lower than exact, unambiguous ones so a page with a proper
# type="submit" button never falls through to a guess.
SUBMIT_SELECTOR_PRIORITY = [
    '[type="submit"]',
    '[role="button"]',
    'text="Submit"',
    'text="Complete Application"',
    'text="Next"',
    'text="Apply"',
]

SUBMIT_SELECTOR_TIMEOUT_MS = 4000    # per-selector wait budget in a single frame
FRAME_SUBMIT_TIMEOUT_MS = 2000       # smaller budget per iframe, since several may be checked
OVERALL_SUBMIT_SEARCH_BUDGET_S = 20  # hard ceiling across main frame + all iframes combined

# injection.py's RED_BORDER normalizes to this rgb() fragment in the DOM
# -- used by the DOM-scan fallback in validate_form_state.
_RED_BORDER_RGB_FRAGMENT = "231, 76, 60"

# --- Verification Gate (post-submit confirmation) ---
# How long to block after a Submit click, waiting for real evidence the
# submission went through, before logging a timeout instead of a false
# "SUBMITTED". This is what prevents main.py's browser.close() from
# firing while the ATS is still processing the request.
VERIFICATION_TIMEOUT_MS = 30000
VERIFICATION_POLL_INTERVAL_MS = 500

# URL substrings commonly present on ATS "thank you" / confirmation pages.
SUCCESS_URL_KEYWORDS = ("thank", "confirm", "success", "submitted", "complete")

# Inline success banners for SPA-style ATS forms that never navigate away.
SUCCESS_SELECTOR_CANDIDATES = [
    'text=/thank you/i',
    'text=/application (submitted|received|complete)/i',
    'text=/successfully submitted/i',
    '[data-testid*="confirmation" i]',
    '[class*="success" i]',
    '[role="alert"]:has-text("submitted")',
]


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def _ensure_log_file_exists() -> None:
    """Create the CSV log with headers if it doesn't already exist."""
    if not LOG_FILE.exists():
        with LOG_FILE.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()


def log_application(url: str, status: str, notes: str = "") -> None:
    """
    Append a single record to applications_log.csv.

    status: e.g. "SUBMITTED", "SUBMIT_FAILED", "REVIEW_ABANDONED", "DRY_RUN"
    """
    _ensure_log_file_exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writerow({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "url": url,
            "status": status,
            "notes": notes,
        })


# --------------------------------------------------------------------------
# Priority-based, frame-aware submit button detection
# --------------------------------------------------------------------------

def find_and_wait_for_submit(context: Page | Frame, timeout_ms: int = SUBMIT_SELECTOR_TIMEOUT_MS) -> Locator | None:
    """
    Iterates SUBMIT_SELECTOR_PRIORITY against a single frame, returning
    the first locator that resolves to a visible and enabled element.
    Matches on substrings/regex (case-insensitive) rather than exact
    text, then falls back to a broader regex pass if the priority list
    finds nothing.

    Note on Shadow DOM: Playwright's css/text selector engines already
    pierce OPEN shadow roots automatically, no special syntax needed.
    CLOSED shadow roots are a genuine browser security boundary no
    automation tool can reach without the page's cooperation — if a site
    uses closed shadow DOM for its submit button, the only reliable path
    is a manual click during the review pause (run_review_and_submit's
    manual-click detector supports this).
    """
    for selector in SUBMIT_SELECTOR_PRIORITY:
        try:
            locator = context.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            if not locator.is_enabled():
                continue

            try:
                button_text = locator.inner_text(timeout=500).strip()
            except PlaywrightError:
                button_text = "<no text>"
            logger.info(f"[find_and_wait_for_submit] Matched {selector!r} -> {button_text!r}")
            return locator

        except PlaywrightTimeoutError:
            continue
        except PlaywrightError:
            continue

    locator = _regex_text_fallback(context, timeout_ms)
    if locator:
        return locator

    return None


def _regex_text_fallback(context: Page | Frame, timeout_ms: int) -> Locator | None:
    """
    Broader, case-insensitive regex pass over button-like elements, tried
    only after the exact priority list is exhausted (more likely to grab
    the wrong element, e.g. "Continue to next step" on a multi-page form
    that isn't the final submit).
    """
    pattern = re.compile(
        r"submit|apply|send application|finish|complete|continue|next",
        re.IGNORECASE,
    )
    try:
        candidates = context.get_by_role("button", name=pattern)
        locator = candidates.first
        locator.wait_for(state="visible", timeout=timeout_ms)
        if locator.is_enabled():
            try:
                button_text = locator.inner_text(timeout=500).strip()
            except PlaywrightError:
                button_text = "<no text>"
            logger.info(f"[_regex_text_fallback] Matched via role=button regex -> {button_text!r}")
            return locator
    except (PlaywrightTimeoutError, PlaywrightError):
        pass

    try:
        candidates = context.get_by_text(pattern)
        locator = candidates.first
        locator.wait_for(state="visible", timeout=timeout_ms)
        if locator.is_enabled():
            try:
                button_text = locator.inner_text(timeout=500).strip()
            except PlaywrightError:
                button_text = "<no text>"
            logger.info(f"[_regex_text_fallback] Matched via text regex -> {button_text!r}")
            return locator
    except (PlaywrightTimeoutError, PlaywrightError):
        pass

    return None


def _dump_visible_buttons(page: Page) -> None:
    """
    Diagnostic for when nothing matched across every strategy and frame:
    prints every visible button-like element so you can either extend
    _regex_text_fallback's pattern or confirm it's something
    automation-inaccessible (closed shadow DOM, canvas UI) needing a
    manual click.
    """
    print("\n" + "-" * 70)
    print("[review_gate] DIAGNOSTIC: visible button-like elements found on page")
    print("-" * 70)
    frames_to_check = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    found_any = False
    for frame in frames_to_check:
        try:
            elements = frame.locator('button, [role="button"], input[type="submit"], input[type="button"]')
            count = elements.count()
        except PlaywrightError:
            continue
        for i in range(min(count, 25)):  # cap output, don't flood the terminal
            try:
                el = elements.nth(i)
                if not el.is_visible():
                    continue
                text = el.inner_text(timeout=500).strip() or "<no text>"
                print(f"  [{frame.url or 'main frame'}] {text!r}")
                found_any = True
            except PlaywrightError:
                continue
    if not found_any:
        print("  (none found -- the button may be in a closed shadow root, "
              "a canvas, or not yet rendered)")
    print("-" * 70 + "\n")


def _locate_submit_with_fallback(page: Page) -> tuple[Locator | None, Frame | None]:
    """
    Tries the main frame first, then every iframe, since many ATS forms
    render the actual form (and its submit button) inside a cross-origin
    iframe rather than the top-level document.

    Raises PlaywrightTimeoutError if the overall search budget is
    exceeded, so the caller can fail soft instead of hanging indefinitely
    across many iframes.
    """
    deadline = time.monotonic() + OVERALL_SUBMIT_SEARCH_BUDGET_S

    def _remaining_ms() -> int:
        return max(500, int((deadline - time.monotonic()) * 1000))

    locator = find_and_wait_for_submit(page, timeout_ms=min(SUBMIT_SELECTOR_TIMEOUT_MS, _remaining_ms()))
    if locator:
        return locator, page.main_frame

    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if time.monotonic() >= deadline:
            raise PlaywrightTimeoutError(
                f"Exceeded {OVERALL_SUBMIT_SEARCH_BUDGET_S}s submit-button search "
                f"budget across the main frame and {len(page.frames) - 1} iframe(s)."
            )
        locator = find_and_wait_for_submit(frame, timeout_ms=min(FRAME_SUBMIT_TIMEOUT_MS, _remaining_ms()))
        if locator:
            logger.info(f"[review_gate] Found submit button inside iframe: {frame.url}")
            return locator, frame

    return None, None


# --------------------------------------------------------------------------
# Timeout-resilient page analysis
# --------------------------------------------------------------------------

def _print_page_analysis(page: Page) -> None:
    """
    Printed instead of crashing when the button/form search times out —
    gives enough context (URL + visible input count) to know where to
    look once manual intervention takes over.
    """
    try:
        input_count = page.locator("input, select, textarea, [role='combobox']").count()
    except PlaywrightError:
        input_count = "unknown"

    try:
        frame_count = len(page.frames)
    except PlaywrightError:
        frame_count = "unknown"

    print("\n" + "=" * 70)
    print("[review_gate] PAGE ANALYSIS (timeout recovery)")
    print("=" * 70)
    print(f"  Current URL:            {page.url}")
    print(f"  Detected input fields:  {input_count}")
    print(f"  Frames on page:         {frame_count}")
    print("  The script could not automatically locate/validate the submit")
    print("  button in time. Pausing for manual intervention instead of")
    print("  crashing -- find and click Submit yourself once you resume.")
    print("=" * 70 + "\n")


# --------------------------------------------------------------------------
# Validation before pause
# --------------------------------------------------------------------------

def validate_form_state(page: Page, injection_report: "InjectionReport | None" = None) -> list[str]:
    """
    Returns human-readable descriptions of fields that still need
    attention, so the reviewer knows what to look for the moment the
    browser pauses.

    Preferred path: if `injection_report` is supplied, reads its results
    directly — accurate, matches exactly what injection.py decided.

    Fallback path: if no injection_report is available, scans the live
    DOM for elements still carrying injection.py's red inline border
    style. Heuristic — can't say WHY a field is flagged, only THAT it is.
    """
    unresolved: list[str] = []

    if injection_report is not None:
        for r in injection_report.results:
            is_flagged = (r.status != "filled") or (r.confidence < CONFIDENCE_THRESHOLD)
            if not is_flagged:
                continue
            if r.error_message:
                reason = r.error_message
            elif not r.value:
                reason = "left blank (no confident match found)"
            else:
                reason = f"low confidence ({r.confidence * 100:.0f}%)"
            unresolved.append(f"{r.label} — {reason}")
        return unresolved

    try:
        flagged_elements = page.eval_on_selector_all(
            "*",
            f"""els => els
                .filter(el => el.style && el.style.borderColor &&
                               el.style.borderColor.includes('{_RED_BORDER_RGB_FRAGMENT}'))
                .map(el => el.getAttribute('aria-label') || el.getAttribute('name') ||
                           el.id || el.tagName)
            """,
        )
        unresolved.extend(f"{name} — flagged red in the DOM" for name in flagged_elements)
    except PlaywrightError as e:
        logger.warning(f"[validate_form_state] DOM fallback scan failed: {e}")

    return unresolved


def _print_validation_warning(unresolved: list[str]) -> None:
    if not unresolved:
        print("[review_gate] All injected fields look resolved — no red-flagged fields found.")
        return

    print("\n" + "!" * 70)
    print(f"[review_gate] WARNING: {len(unresolved)} field(s) still need your attention:")
    for item in unresolved:
        print(f"  - {item}")
    print("!" * 70 + "\n")


# --------------------------------------------------------------------------
# The Review Gate itself, with manual-click detection
# --------------------------------------------------------------------------

_CLICK_FLAG_JS_VAR = "__jobAgentSubmitClicked"


def _arm_manual_click_detector(page: Page, submit_locator: Locator) -> None:
    """
    Attaches a one-time listener to the resolved submit button before the
    pause, so a manual click during the pause (rather than the script
    clicking it after resume) is still detected and logged correctly.
    """
    try:
        page.evaluate(f"() => {{ window.{_CLICK_FLAG_JS_VAR} = false; }}")
        submit_locator.evaluate(
            f"(el) => el.addEventListener('click', "
            f"() => {{ window.{_CLICK_FLAG_JS_VAR} = true; }}, {{ once: true }})"
        )
    except PlaywrightError as e:
        logger.warning(f"[review_gate] Could not arm manual-click detector: {e}")


def _was_submit_clicked_manually(page: Page) -> bool:
    try:
        return bool(page.evaluate(f"() => window.{_CLICK_FLAG_JS_VAR} === true"))
    except PlaywrightError:
        return False


def open_review_gate(page: Page) -> None:
    """
    Freezes automation and hands control to the human reviewer.
    page.pause() opens the Playwright Inspector and halts execution
    indefinitely — nothing after this call runs until Resume is clicked
    (or stepped through). It will not resume on a timer and will not
    submit anything on its own.
    """
    print("\n" + "=" * 70)
    print("REVIEW GATE: Application form has been filled and styled.")
    print("  GREEN borders = AI confidence > 90%")
    print("  RED borders   = AI confidence <= 90% or field left empty")
    print("Playwright Inspector is now open. The script is PAUSED.")
    print("You may either:")
    print("  (a) click Submit yourself in the browser, then click Resume, or")
    print("  (b) just click Resume and confirm submission at this terminal.")
    print("=" * 70 + "\n")

    page.pause()  # hard stop; nothing below runs until resumed


# --------------------------------------------------------------------------
# Verification Gate: block until the submission is actually confirmed
# --------------------------------------------------------------------------

def verify_submission(page: Page, timeout_ms: int = VERIFICATION_TIMEOUT_MS) -> tuple[bool, str]:
    """
    Blocking verification gate, called immediately after any Submit
    click (script- or human-initiated). Does not return until either
    real evidence of success appears or timeout_ms elapses.

    This closes the race condition where main.py's browser.close() (in a
    `finally` block) could fire while an ATS submission is still in
    flight — as long as this function blocks on real evidence, the
    browser can't close out from under an in-flight request.

    Uses a polling loop rather than a fixed sleep, since a fixed delay is
    a guess with no feedback: too short risks logging SUCCESS mid-flight,
    too long wastes time on every run including fast ones. Checks both a
    URL keyword match AND a set of common success-element selectors on
    every poll, since many ATS forms confirm via an inline AJAX banner
    without changing the URL at all.

    Returns:
        (True, description) once success evidence is found.
        (False, description) if timeout_ms elapses with no confirmation.
    """
    deadline = time.monotonic() + (timeout_ms / 1000)

    while True:
        current_url = page.url.lower()
        matched_keyword = next((kw for kw in SUCCESS_URL_KEYWORDS if kw in current_url), None)
        if matched_keyword:
            return True, f"URL contains {matched_keyword!r}: {page.url}"

        for selector in SUCCESS_SELECTOR_CANDIDATES:
            try:
                candidate = page.locator(selector).first
                if candidate.is_visible(timeout=200):
                    text = candidate.inner_text(timeout=200).strip()
                    return True, f"Success element matched {selector!r} -> {text!r}"
            except PlaywrightError:
                continue

        if time.monotonic() >= deadline:
            return False, (
                f"No success URL keyword or on-page confirmation element "
                f"detected within {timeout_ms}ms (last seen URL: {page.url})"
            )

        page.wait_for_timeout(VERIFICATION_POLL_INTERVAL_MS)


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------

def submit_application(page: Page, submit_locator: Locator | None = None, dry_run: bool = False) -> bool:
    """
    Clicks the final Submit/Apply button, then blocks on
    verify_submission() before logging or returning. Call this after
    resuming from page.pause() and reviewing the form.

    Args:
        page: the active Playwright Page.
        submit_locator: a pre-resolved Locator (preferred — avoids
            re-searching after the page may have changed during review).
        dry_run: if True, locate the button and log intent without clicking.

    Returns:
        True if submission was clicked and verified (or dry run).
        False if no button was found, the click failed, or verification
        timed out without confirmation.
    """
    url = page.url

    if submit_locator is None:
        try:
            submit_locator, _frame = _locate_submit_with_fallback(page)
        except PlaywrightTimeoutError as e:
            logger.warning(f"[submit_application] Submit search timed out: {e}")
            submit_locator = None

    if submit_locator is None:
        print("[submit_application] No submit button found. Nothing clicked.")
        log_application(url, status="SUBMIT_FAILED", notes="Submit button not found")
        return False

    if dry_run:
        print(f"[submit_application] DRY RUN — would click: {submit_locator}")
        log_application(url, status="DRY_RUN", notes="No click performed")
        return True

    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        # Non-fatal -- some ATS pages keep background polling alive and
        # never truly go idle.
        print("[submit_application] networkidle wait timed out; continuing.")

    try:
        submit_locator.scroll_into_view_if_needed()
        submit_locator.click()

        print("[submit_application] Clicked Submit — verifying before returning control...")
        verified, detail = verify_submission(page)

        if verified:
            print(f"[submit_application] Verified: {detail}")
            log_application(url, status="SUBMITTED", notes=detail)
            return True
        else:
            print(f"[submit_application] WARNING — could not verify submission: {detail}")
            log_application(url, status="SUBMIT_TIMEOUT", notes=detail)
            return False

    except Exception as exc:  # noqa: BLE001 — log any failure, don't crash
        print(f"[submit_application] Click failed: {exc}")
        log_application(url, status="SUBMIT_FAILED", notes=str(exc))
        return False


# --------------------------------------------------------------------------
# Full Phase 5 integration point
# --------------------------------------------------------------------------

def run_review_and_submit(page: Page, injection_report: "InjectionReport | None" = None) -> None:
    """
    Drop-in call for the end of the pipeline:

        inject_matched_fields(page, matched_fields)          # Phase 4
        run_review_and_submit(page, injection_report)         # Phase 5 <- this

    `injection_report` is optional so this still runs against a caller
    that only passes `page`, but pass it whenever available — validation
    is far more accurate with real Phase 4 results than the DOM-scan
    fallback.

    Flow:
      1. validate_form_state() checks for unresolved fields and warns
         before the pause.
      2. The submit button is located and armed with a manual-click
         detector before pausing.
      3. open_review_gate(page) freezes everything on page.pause().
      4. The reviewer inspects/edits the form live — either clicking
         Submit themselves, or just resuming.
      5. On resume: a manual Submit click is detected and logged
         immediately; otherwise one more explicit CLI checkpoint ("Type
         'Y' to submit") gates the script's own click.

    If button search or validation times out, this catches it, prints a
    page analysis, and still pauses for manual intervention rather than
    crashing or leaving the browser in an unknown state.
    """
    submit_locator: Locator | None = None

    try:
        unresolved = validate_form_state(page, injection_report)
        _print_validation_warning(unresolved)

        submit_locator, _frame = _locate_submit_with_fallback(page)
        if submit_locator:
            _arm_manual_click_detector(page, submit_locator)
        else:
            logger.warning(
                "[review_gate] No submit button located before pause — "
                "manual-click detection disabled for this run; you'll get "
                "the CLI prompt path after resuming instead."
            )

    except PlaywrightTimeoutError as e:
        logger.warning(f"[review_gate] Timed out during pre-pause setup: {e}")
        _dump_visible_buttons(page)
        _print_page_analysis(page)
        submit_locator = None

    open_review_gate(page)  # blocks here until resumed manually

    # --- Everything below only executes after the user resumes ---

    if submit_locator is not None and _was_submit_clicked_manually(page):
        print(
            "[run_review_and_submit] Detected a manual Submit click during "
            "the pause. Verifying before returning control..."
        )
        verified, detail = verify_submission(page)
        status = "SUBMITTED" if verified else "SUBMIT_TIMEOUT"
        if verified:
            print(f"[run_review_and_submit] Verified: {detail}")
        else:
            print(f"[run_review_and_submit] WARNING — could not verify submission: {detail}")
        log_application(
            page.url,
            status=status,
            notes=f"Manual click during review pause; {detail}",
        )
        return

    confirm = input(
        "\nResumed from Inspector. Type 'Y' and press Enter to submit "
        "this application, or anything else to abort: "
    ).strip().lower()

    if confirm == "y":
        submit_application(page, submit_locator=submit_locator)
    else:
        print("[run_review_and_submit] Submission aborted by user.")
        log_application(page.url, status="REVIEW_ABANDONED", notes="User declined at CLI prompt")


# --------------------------------------------------------------------------
# Standalone manual trigger (for calling from the Inspector console)
# --------------------------------------------------------------------------
#
# To skip the CLI prompt and trigger submission directly from the
# Playwright Inspector's console while paused, call
# submit_application(page) directly there instead of using
# run_review_and_submit(). Both paths log to applications_log.csv the
# same way.