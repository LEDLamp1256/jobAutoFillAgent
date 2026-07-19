"""
Phase 4: Injection and Visual UI Layer
----------------------------------------
Consumes the JSON output from the Phase 3 AI Matching Engine and:
  1. Populates form fields on the live page via Playwright.
  2. Visually highlights every touched field so the human reviewer can
     scan the page at a glance (green = high confidence, red = needs review).
  3. Handles missing / stale DOM elements gracefully so one bad selector
     doesn't kill the whole run.

Expected input shape (from the matching engine), a list of field records:

    [
        {
            "selector": "#firstName",          # CSS selector or Playwright locator string
            "field_type": "text",              # "text" | "textarea" | "tel" | "email" | "url" |
                                                # "number" | "select" | "checkbox" | "radio" |
                                                # "file" | "date" | "custom_dropdown" |
                                                # "custom_date_picker"
            "value": "Jordan",                 # value to inject (label/text for select,
                                                # ISO YYYY-MM-DD for dates, absolute path
                                                # for file uploads)
            "confidence": 0.97,                # float 0.0 - 1.0
            "label": "First Name"              # human-readable label, for logging/summary
        },
        ...
    ]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("injection_engine")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

CONFIDENCE_THRESHOLD = 0.90  # matches the >90% requirement

GREEN_BORDER = "3px solid #2ecc71"
RED_BORDER = "3px solid #e74c3c"

# Playwright's default timeout is 30s; for a fast local fill loop we want to
# fail quickly on missing elements rather than stall the whole batch.
ELEMENT_TIMEOUT_MS = 4000

# "Resilient Injection" uses a longer timeout deliberately -- this path only
# runs for fields the AI already matched with real confidence, so it's
# worth waiting out slower modern pages (animations, lazy hydration,
# elements attached before they're truly interactable) rather than giving
# up at the same 4s budget used for a first-pass existence check.
RESILIENT_TIMEOUT_MS = 10_000


class FieldType(str, Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    TEL = "tel"
    EMAIL = "email"
    URL = "url"
    NUMBER = "number"
    SELECT = "select"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    RADIO_GROUP = "radio_group"
    FILE = "file"
    DATE = "date"
    CUSTOM_DROPDOWN = "custom_dropdown"
    CUSTOM_DATE_PICKER = "custom_date_picker"


# HTML5 input types that Playwright fills identically to a plain text
# field via locator.fill(). Add new types here rather than editing the
# injection loop's conditional logic.
TEXT_LIKE_TYPES = {
    FieldType.TEXT,
    FieldType.TEXTAREA,
    FieldType.TEL,
    FieldType.EMAIL,
    FieldType.URL,
    FieldType.NUMBER,
}


@dataclass
class FieldResult:
    """Outcome of attempting to inject a single field, used for the CLI review summary."""
    label: str
    selector: str
    value: Any
    confidence: float
    status: str  # "filled" | "skipped" | "error"
    error_message: str | None = None


@dataclass
class InjectionReport:
    """Aggregate result of a full injection pass, handed to the Review Gate."""
    results: list[FieldResult] = field(default_factory=list)

    @property
    def high_confidence_count(self) -> int:
        return sum(1 for r in self.results if r.status == "filled" and r.confidence >= CONFIDENCE_THRESHOLD)

    @property
    def needs_review_count(self) -> int:
        return sum(1 for r in self.results if r.status != "filled" or r.confidence < CONFIDENCE_THRESHOLD)

    def print_summary(self) -> None:
        """CLI-friendly summary consumed by the Phase 5 Review Gate."""
        print("\n" + "=" * 60)
        print("INJECTION SUMMARY")
        print("=" * 60)
        for r in self.results:
            flag = "✅" if (r.status == "filled" and r.confidence >= CONFIDENCE_THRESHOLD) else "⚠️ "
            conf_pct = f"{r.confidence * 100:.0f}%"
            line = f"{flag} [{r.status.upper():7}] {r.label:30} conf={conf_pct:5} -> {r.value!r}"
            if r.error_message:
                line += f"  (error: {r.error_message})"
            print(line)
        print("-" * 60)
        print(f"High-confidence fills: {self.high_confidence_count}")
        print(f"Needs manual review:   {self.needs_review_count}")
        print("=" * 60 + "\n")


# --------------------------------------------------------------------------- #
# Selector safety
# --------------------------------------------------------------------------- #

def _sanitize_selector(selector: str) -> str:
    """
    Dynamically-generated IDs from modern JS frameworks / ATS platforms
    (Ashby, Workday, Radix-based UIs, etc.) are often invalid as unescaped
    CSS identifiers -- e.g. an ID starting with a digit like
    "8548f6fb-ebd9-47cd-9064-bc9f6d0ceb88_input" is a SyntaxError when
    interpolated directly into "#8548f6fb-...".

    Rather than hand-rolling CSS.escape()-style character escaping, this
    converts a raw "#id" selector into an attribute selector,
    '[id="..."]', instead. Attribute selector *values* are parsed as
    quoted strings, not CSS identifiers, so none of the identifier-escaping
    rules (no leading digit, no bare ':', etc.) apply -- only the quote
    character itself needs escaping. Any selector that isn't a bare "#id"
    form (attribute selectors, nth-of-type paths, etc.) is returned
    unchanged, since those are already safe.
    """
    if selector.startswith("#") and len(selector) > 1:
        raw_id = selector[1:]
        escaped_id = raw_id.replace("\\", "\\\\").replace('"', '\\"')
        return f'[id="{escaped_id}"]'
    return selector


# --------------------------------------------------------------------------- #
# Highlighting
# --------------------------------------------------------------------------- #

def _apply_highlight(page: Page, selector: str, border_css: str) -> None:
    """
    Injects an inline border style onto the target element via page.evaluate().
    Using evaluate (rather than Playwright's own style APIs) lets us set
    !important and avoid fighting the page's own CSS specificity.
    """
    safe_selector = _sanitize_selector(selector)
    page.evaluate(
        """([sel, border]) => {
            const el = document.querySelector(sel);
            if (el) {
                el.style.setProperty('border', border, 'important');
                el.style.setProperty('border-radius', '4px', 'important');
                el.style.setProperty('box-shadow', '0 0 4px ' + border.split(' ')[2], 'important');
            }
        }""",
        [safe_selector, border_css],
    )


def highlight_field(page: Page, selector: str, confidence: float, filled: bool) -> None:
    """Pick green/red based on confidence + fill success, then apply it."""
    is_confident = filled and confidence >= CONFIDENCE_THRESHOLD
    border = GREEN_BORDER if is_confident else RED_BORDER
    try:
        _apply_highlight(page, selector, border)
    except PlaywrightError as e:
        # Highlighting is best-effort — never let a styling failure abort the run.
        logger.warning(f"Could not highlight '{selector}': {e}")


# --------------------------------------------------------------------------- #
# Custom widget interaction (non-native dropdowns / date pickers)
# --------------------------------------------------------------------------- #

def _select_custom_dropdown_option(page: Page, locator, value: str) -> None:
    """
    Best-effort interaction for non-native dropdowns (Workday-style div/
    button combos identified by jobScraper's _scrape_custom_dropdowns).
    Clicks to open the menu, then clicks the option whose visible text
    matches `value`.

    This is inherently heuristic — custom dropdown markup varies widely
    across ATS platforms. If a given site's widget doesn't expose proper
    ARIA roles or matching visible text, this raises and the field gets
    flagged red for manual review rather than silently failing or
    guessing wrong.
    """
    locator.click(timeout=RESILIENT_TIMEOUT_MS)

    try:
        option = page.get_by_role("option", name=value, exact=False).first
        option.wait_for(state="visible", timeout=RESILIENT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Widget doesn't use proper ARIA roles — fall back to plain text.
        option = page.get_by_text(value, exact=False).first
        option.wait_for(state="visible", timeout=RESILIENT_TIMEOUT_MS)

    option.click(timeout=RESILIENT_TIMEOUT_MS)


def _select_custom_date(page: Page, locator, iso_date: str) -> None:
    """
    Best-effort interaction for calendar-widget date pickers that aren't a
    native <input type="date">. Tries typing the ISO date directly first —
    many widgets accept typed input even though they also render a popup
    calendar. If that fails, falls back to clicking the page.

    LIMITATION: the calendar-click fallback only clicks a visible cell
    matching the target day-of-month — it does NOT navigate month/year, so
    it only works if the calendar already opens on the correct month. Full
    calendar navigation is too site-specific to generalize; treat this as
    a starting point to extend per-ATS if you hit a widget it can't handle.
    """
    try:
        locator.fill(iso_date, timeout=RESILIENT_TIMEOUT_MS)
        return
    except PlaywrightError:
        pass  # Not a fillable input — fall through to calendar-click.

    locator.click(timeout=RESILIENT_TIMEOUT_MS)
    day = str(int(iso_date.split("-")[-1]))  # "05" -> "5", matches typical calendar cell text
    day_selector = f'[role="gridcell"]:has-text("{day}"), td:has-text("{day}")'
    page.wait_for_selector(day_selector, state="visible", timeout=RESILIENT_TIMEOUT_MS)
    page.locator(day_selector).first.click(timeout=RESILIENT_TIMEOUT_MS)


# --------------------------------------------------------------------------- #
# Autofill-trigger detection (hard block, not just a prompt instruction)
# --------------------------------------------------------------------------- #

# Phrases indicating an ATS's own "parse my resume and fill out the whole
# form for me" button, rather than a genuine data-entry field or a plain
# resume/CV file upload input. Match is substring-based and case-insensitive
# on the resolved label -- deliberately broad, since the cost of a false
# positive (skipping a legitimate field with unusual wording) is far lower
# than the cost of a false negative (letting the ATS's parser overwrite the
# whole form we just carefully built from config.json).
_AUTOFILL_TRIGGER_PHRASES = (
    "autofill with resume", "autofill from resume", "auto-fill with resume",
    "auto-fill from resume", "autofill using resume", "parse resume",
    "use resume to autofill", "autofill application", "import from resume",
    "scan resume", "upload resume to autofill", "fill from resume",
)


def _is_autofill_trigger(label: str) -> bool:
    normalized = " ".join((label or "").strip().lower().split())
    return any(phrase in normalized for phrase in _AUTOFILL_TRIGGER_PHRASES)


# --------------------------------------------------------------------------- #
# Resilient Injection: forgiving fill/select + failure debugging
# --------------------------------------------------------------------------- #

def _log_outer_html_for_debug(locator, label: str) -> None:
    """
    On injection failure, dump the element's outerHTML so you can see WHY
    it wasn't interactable (disabled, hidden via a class rather than
    inline style, covered by an overlay, zero-size, etc.) without having
    to reproduce the failure in a headed browser yourself.
    """
    try:
        html = locator.evaluate("el => el.outerHTML", timeout=1000)
        # Can be huge for container-like custom widgets -- cap it so one
        # bad element doesn't flood the log.
        if html and len(html) > 800:
            html = html[:800] + "... [truncated]"
        logger.warning(f"[{label}] outerHTML at failure time: {html}")
    except PlaywrightError as e:
        logger.warning(f"[{label}] Could not retrieve outerHTML for debugging (element may not exist at all): {e}")


def _resilient_fill(locator, value: str, label: str) -> None:
    """
    Fill with a click-to-focus fallback. Some custom-styled inputs
    (dropdown-style text fields, masked inputs, inputs that only start
    accepting typed input after a real click/focus event) reject a direct
    fill() even though the element is visible and attached -- this is
    exactly the "100% confidence but still times out" symptom, since the
    AI's confidence has nothing to do with whether the widget cooperates
    with a synthetic fill.
    """
    try:
        locator.fill(value, timeout=RESILIENT_TIMEOUT_MS)
        return
    except PlaywrightError as e:
        logger.info(f"[{label}] Direct fill failed ({e}); retrying via click-to-focus fallback.")

    locator.click(timeout=RESILIENT_TIMEOUT_MS)
    locator.fill(value, timeout=RESILIENT_TIMEOUT_MS)


def _resilient_select(locator, value: str, label: str) -> None:
    """
    select_option() with the same click-to-focus fallback as
    _resilient_fill, for custom-styled <select> elements that need a
    click to register focus/open before an option becomes selectable.
    Tries matching by visible label first, falls back to raw value, in
    both the direct and fallback attempts.
    """
    try:
        try:
            locator.select_option(label=value, timeout=RESILIENT_TIMEOUT_MS)
        except PlaywrightError:
            locator.select_option(value=value, timeout=RESILIENT_TIMEOUT_MS)
        return
    except PlaywrightError as e:
        logger.info(f"[{label}] select_option failed ({e}); retrying via click-to-focus fallback.")

    locator.click(timeout=RESILIENT_TIMEOUT_MS)
    try:
        locator.select_option(label=value, timeout=RESILIENT_TIMEOUT_MS)
    except PlaywrightError:
        locator.select_option(value=value, timeout=RESILIENT_TIMEOUT_MS)


# --------------------------------------------------------------------------- #
# Field injection
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# radio_group injection (no single element -- resolved via option_selectors)
# --------------------------------------------------------------------------- #

def _inject_radio_group(page: Page, record: dict[str, Any], result: FieldResult) -> FieldResult:
    """
    Handles field_type == "radio_group". There is no single element to
    locate here by design (jobScraper's _scrape_radio_groups sets
    selector_hint to None, since no one element represents a whole
    group) -- instead, option_selectors maps each possible option's text
    to that SPECIFIC radio's real selector, and we click whichever one
    matches the AI's chosen value.
    """
    label = result.label
    confidence = result.confidence
    value = result.value

    if value is None or value == "":
        return result  # nothing chosen -- leave as "skipped", nothing to highlight (no single element)

    value = str(value)
    option_selectors: dict[str, str] = record.get("option_selectors", {})

    chosen_selector = option_selectors.get(value)
    if not chosen_selector:
        # The LLM sometimes normalizes casing/whitespace slightly even
        # when it's supposed to copy an option's text verbatim -- try a
        # case/whitespace-insensitive match before giving up.
        normalized_value = " ".join(value.strip().lower().split())
        for option_text, sel in option_selectors.items():
            if " ".join(option_text.strip().lower().split()) == normalized_value:
                chosen_selector = sel
                break

    if not chosen_selector:
        msg = f"AI chose {value!r}, which isn't one of the scraped options: {list(option_selectors.keys())}"
        logger.warning(f"[{label}] {msg}")
        result.status = "error"
        result.error_message = msg
        return result

    safe_selector = _sanitize_selector(chosen_selector)
    result.selector = safe_selector
    locator = page.locator(safe_selector).first

    try:
        try:
            locator.scroll_into_view_if_needed(timeout=RESILIENT_TIMEOUT_MS)
        except PlaywrightError:
            pass
        locator.wait_for(state="visible", timeout=RESILIENT_TIMEOUT_MS)
        locator.wait_for(state="attached", timeout=RESILIENT_TIMEOUT_MS)
        locator.check(timeout=RESILIENT_TIMEOUT_MS)

        result.status = "filled"
        highlight_field(page, safe_selector, confidence, filled=True)

    except PlaywrightTimeoutError:
        msg = f"Timed out selecting radio option {value!r} within {RESILIENT_TIMEOUT_MS}ms"
        logger.warning(f"[{label}] {msg}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = msg
        highlight_field(page, safe_selector, confidence, filled=False)

    except PlaywrightError as e:
        logger.warning(f"[{label}] Playwright error selecting radio option: {e}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = str(e)
        highlight_field(page, safe_selector, confidence, filled=False)

    return result


def _inject_single_field(page: Page, record: dict[str, Any]) -> FieldResult:
    """
    Attempts to fill one field according to its declared type.
    Returns a FieldResult regardless of success/failure — callers should
    never need to catch exceptions from this function directly.
    """
    raw_selector = record.get("selector")
    field_type = record.get("field_type", "text")
    value = record.get("value")
    confidence = float(record.get("confidence", 0.0))
    label = record.get("label") or raw_selector or "UNKNOWN_FIELD"

    result = FieldResult(
        label=label,
        selector=raw_selector or "(radio_group -- see option_selectors)",
        value=value,
        confidence=confidence,
        status="skipped",
    )

    # HARD BLOCK -- never interact with an ATS's own "autofill from resume"
    # trigger, regardless of what the matching engine said. These buttons
    # hand the entire form over to the site's own resume parser, silently
    # overwriting every config-driven value we just carefully computed.
    # This is a code-level check, not just a prompt instruction, because
    # LLM instruction-following on this specific distinction isn't reliable
    # enough to trust alone -- see aiMatcher.py rule 14 for the prompt-side
    # half of this fix.
    if _is_autofill_trigger(label):
        logger.warning(
            f"[{label}] Refusing to interact -- this looks like an ATS "
            f"auto-parse-from-resume trigger, not a genuine data field. Skipping."
        )
        result.status = "skipped"
        result.error_message = "Blocked: appears to be an autofill-from-resume trigger, not a data field"
        if raw_selector:
            highlight_field(page, raw_selector, confidence, filled=False)
        return result

    # radio_group has NO single element/selector by design -- jobScraper's
    # _scrape_radio_groups sets selector_hint to None because no one
    # element represents the whole group. It gets its own dedicated path
    # via option_selectors, resolved BEFORE any of the generic
    # sanitize/locate logic below, which assumes `selector` is always a
    # real string. (Previously, this fell through to using field_id
    # itself as a literal CSS selector, which is a human-readable string
    # like "authorized_no_cpt_opt_needed" -- Playwright then searched for
    # a nonexistent tag and timed out. Fixed in main.py too.)
    if field_type == FieldType.RADIO_GROUP:
        return _inject_radio_group(page, record, result)

    if not raw_selector:
        msg = f"No selector available for field_type={field_type!r} -- this indicates a scraper bug, not an expected gap"
        logger.error(f"[{label}] {msg}")
        result.status = "error"
        result.error_message = msg
        return result

    # Dynamically-generated IDs (UUIDs, hashes, IDs starting with a digit)
    # are invalid as unescaped CSS identifiers and throw a SyntaxError from
    # querySelector/locator if used raw as "#id". Sanitize once here so
    # every use below (wait_for_selector, locator, and highlight_field's
    # document.querySelector) sees a safe selector.
    selector = _sanitize_selector(raw_selector)
    result.selector = selector

    # Nothing to inject (matching engine explicitly returned no value / null match)
    if value is None or value == "":
        highlight_field(page, selector, confidence, filled=False)
        return result

    # Resolved once up front so every failure branch below can attempt to
    # log its outerHTML for debugging, regardless of which step failed.
    locator = page.locator(selector).first

    try:
        # Resilient Injection wait sequence, in order:
        #   1. scroll_into_view_if_needed() -- element may be technically
        #      interactable but off-screen, which some frameworks treat as
        #      "not ready" until scrolled into the viewport.
        #   2. wait_for(state="visible") -- confirms it's actually rendered
        #      and shown, not just present in the DOM.
        #   3. wait_for(state="attached") -- belt-and-suspenders re-check
        #      that it's still in the DOM (covers the rare case of a
        #      re-render between steps 1-2 and the fill attempt below).
        # All three use RESILIENT_TIMEOUT_MS (10s), not the tighter
        # ELEMENT_TIMEOUT_MS -- these are fields the AI already matched
        # with real confidence, worth spending more time on.
        try:
            locator.scroll_into_view_if_needed(timeout=RESILIENT_TIMEOUT_MS)
        except PlaywrightError:
            pass  # not fatal -- e.g. already in view, or inside a container
                  # that doesn't support programmatic scrolling
        locator.wait_for(state="visible", timeout=RESILIENT_TIMEOUT_MS)
        locator.wait_for(state="attached", timeout=RESILIENT_TIMEOUT_MS)

        if field_type in TEXT_LIKE_TYPES or field_type == FieldType.DATE:
            # Native <input type="date"> also accepts a plain ISO string
            # via fill(), so DATE shares the same resilient path as the
            # text-like types rather than needing its own branch.
            _resilient_fill(locator, str(value), label)

        elif field_type == FieldType.FILE:
            file_path = Path(str(value)).expanduser()
            if not file_path.is_file():
                raise FileNotFoundError(f"Resume file not found at: {file_path}")
            locator.set_input_files(str(file_path), timeout=RESILIENT_TIMEOUT_MS)

        elif field_type == FieldType.SELECT:
            _resilient_select(locator, str(value), label)

        elif field_type == FieldType.CUSTOM_DROPDOWN:
            _select_custom_dropdown_option(page, locator, str(value))

        elif field_type == FieldType.CUSTOM_DATE_PICKER:
            _select_custom_date(page, locator, str(value))

        elif field_type == FieldType.CHECKBOX:
            should_check = str(value).lower() in ("true", "yes", "1")
            if should_check:
                locator.check(timeout=RESILIENT_TIMEOUT_MS)
            else:
                locator.uncheck(timeout=RESILIENT_TIMEOUT_MS)

        elif field_type == FieldType.RADIO:
            locator.check(timeout=RESILIENT_TIMEOUT_MS)

        else:
            raise ValueError(f"Unsupported field_type: {field_type}")

        result.status = "filled"
        highlight_field(page, selector, confidence, filled=True)

    except FileNotFoundError as e:
        logger.warning(f"[{label}] {e}")
        result.status = "error"
        result.error_message = str(e)
        highlight_field(page, selector, confidence, filled=False)

    except PlaywrightTimeoutError:
        msg = f"Timed out waiting for element (not found or not interactable) within {RESILIENT_TIMEOUT_MS}ms"
        logger.warning(f"[{label}] {msg}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = msg
        highlight_field(page, selector, confidence, filled=False)

    except PlaywrightError as e:
        logger.warning(f"[{label}] Playwright error: {e}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = str(e)
        highlight_field(page, selector, confidence, filled=False)

    except Exception as e:  # noqa: BLE001 - defensive catch-all; we log and continue the batch
        logger.error(f"[{label}] Unexpected error: {e}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = str(e)
        highlight_field(page, selector, confidence, filled=False)

    return result


def inject_matched_fields(page: Page, matched_fields: list[dict[str, Any]]) -> InjectionReport:
    """
    Main entry point for Phase 4.

    Iterates every field the AI matching engine mapped, fills it on the live
    page, and applies a visual confidence border. A single field's failure
    never stops the batch — every field is attempted and logged.

    Args:
        page: An active Playwright Page already navigated to the application form.
        matched_fields: The list of field-mapping dicts produced by Phase 3.

    Returns:
        InjectionReport summarizing every field's outcome, ready to hand
        to the Phase 5 Review Gate (CLI prompt or page.pause()).
    """
    report = InjectionReport()

    for record in matched_fields:
        if "selector" not in record:
            logger.warning(f"Skipping malformed record (missing 'selector'): {record}")
            continue

        field_result = _inject_single_field(page, record)
        report.results.append(field_result)

    logger.info(
        f"Injection pass complete: {report.high_confidence_count} high-confidence, "
        f"{report.needs_review_count} flagged for review."
    )
    return report


# --------------------------------------------------------------------------- #
# Example usage (wire this into your Phase 5 Review Gate)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    from playwright.sync_api import sync_playwright

    # Example payload — in production this comes straight from your Ollama
    # matching engine's parsed JSON response.
    example_matched_fields = [
        {
            "selector": "#firstName",
            "field_type": "text",
            "value": "Jordan",
            "confidence": 0.98,
            "label": "First Name",
        },
        {
            "selector": "#email",
            "field_type": "email",
            "value": "jordan@example.com",
            "confidence": 1.0,
            "label": "Email Address",
        },
        {
            "selector": "#phone",
            "field_type": "tel",
            "value": "555-123-4567",
            "confidence": 1.0,
            "label": "Phone Number",
        },
        {
            "selector": "#coverLetter",
            "field_type": "textarea",
            "value": "I'm excited to apply because...",
            "confidence": 0.62,
            "label": "Cover Letter",
        },
        {
            "selector": "#stateDropdown",
            "field_type": "select",
            "value": "California",
            "confidence": 0.95,
            "label": "State",
        },
        {
            "selector": "#nonExistentField",
            "field_type": "text",
            "value": "test",
            "confidence": 0.80,
            "label": "Ghost Field",
        },
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("about:blank")
        page.set_content(
            """
            <form>
                <input id="firstName" type="text" />
                <input id="email" type="email" />
                <input id="phone" type="tel" />
                <textarea id="coverLetter"></textarea>
                <select id="stateDropdown">
                    <option value="CA">California</option>
                    <option value="NY">New York</option>
                </select>
            </form>
            """
        )

        report = inject_matched_fields(page, example_matched_fields)
        report.print_summary()

        # Hand off to Review Gate (Phase 5): either page.pause() for a
        # headed manual check, or a CLI y/n prompt before submit.
        page.pause()
        browser.close()