"""
Uses the JSON output from the Phase 3 AI Matching Engine and:
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

import difflib
import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Frame, Locator, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("injection_engine")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

CONFIDENCE_THRESHOLD = 0.90  # matches the >90% requirement

GREEN_BORDER = "3px solid #2ecc71"
RED_BORDER = "3px solid #e74c3c"

# Fast fail budget for a first-pass existence check.
ELEMENT_TIMEOUT_MS = 4000

# Longer budget for fields the AI already matched with real confidence —
# worth waiting out slower modern pages (animations, lazy hydration).
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


# HTML5 input types filled identically to a plain text field via locator.fill().
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
    Framework-generated IDs (Ashby, Workday, Radix UI, etc.) are often
    invalid as unescaped CSS identifiers -- e.g. an ID starting with a
    digit is a SyntaxError when interpolated into "#id". Converts a bare
    "#id" selector into an attribute selector '[id="..."]' instead, since
    attribute selector values are quoted strings and only the quote
    character needs escaping. Any other selector shape is returned as-is.
    """
    if selector.startswith("#") and len(selector) > 1:
        raw_id = selector[1:]
        escaped_id = raw_id.replace("\\", "\\\\").replace('"', '\\"')
        return f'[id="{escaped_id}"]'
    return selector


# --------------------------------------------------------------------------- #
# Highlighting
# --------------------------------------------------------------------------- #

def _apply_highlight(locator: Locator, border_css: str) -> None:
    """Applies a confidence-colored border directly to an already-resolved element."""
    locator.evaluate(
        """(el, border) => {
            el.style.setProperty('border', border, 'important');
            el.style.setProperty('border-radius', '4px', 'important');
            el.style.setProperty('box-shadow', '0 0 4px ' + border.split(' ')[2], 'important');
        }""",
        border_css,
    )


def highlight_field(locator: Locator | None, confidence: float, filled: bool) -> None:
    """Picks green/red based on confidence + fill success. No-ops if locator is None."""
    if locator is None:
        return
    is_confident = filled and confidence >= CONFIDENCE_THRESHOLD
    border = GREEN_BORDER if is_confident else RED_BORDER
    try:
        _apply_highlight(locator, border)
    except PlaywrightError as e:
        logger.warning(f"Could not highlight element: {e}")


# --------------------------------------------------------------------------- #
# Custom widget interaction (non-native dropdowns / date pickers)
# --------------------------------------------------------------------------- #

def _select_custom_dropdown_option(page: Page, locator, value: str) -> None:
    """
    Best-effort interaction for non-native dropdowns (div/button combos):
    clicks to open the menu, then clicks the option whose visible text
    matches `value`. Heuristic by nature; if the widget's markup doesn't
    match, this raises and the field is flagged red for manual review.
    """
    locator.click(timeout=RESILIENT_TIMEOUT_MS)

    try:
        option = page.get_by_role("option", name=value, exact=False).first
        option.wait_for(state="visible", timeout=RESILIENT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        option = page.get_by_text(value, exact=False).first
        option.wait_for(state="visible", timeout=RESILIENT_TIMEOUT_MS)

    option.click(timeout=RESILIENT_TIMEOUT_MS)


def _select_custom_date(page: Page, locator, iso_date: str) -> None:
    """
    Best-effort interaction for calendar-widget date pickers. Tries typing
    the ISO date directly first, then falls back to clicking a matching
    day cell.

    LIMITATION: the calendar-click fallback only clicks a visible cell for
    the target day-of-month — it does not navigate month/year, so it only
    works if the calendar already opens on the correct month.
    """
    try:
        locator.fill(iso_date, timeout=RESILIENT_TIMEOUT_MS)
        return
    except PlaywrightError:
        pass

    locator.click(timeout=RESILIENT_TIMEOUT_MS)
    day = str(int(iso_date.split("-")[-1]))
    day_selector = f'[role="gridcell"]:has-text("{day}"), td:has-text("{day}")'
    page.wait_for_selector(day_selector, state="visible", timeout=RESILIENT_TIMEOUT_MS)
    page.locator(day_selector).first.click(timeout=RESILIENT_TIMEOUT_MS)


# Words a second-step confirm/attach button typically carries, for
# two-stage upload widgets (see _click_upload_confirm_if_present).
_UPLOAD_CONFIRM_WORDS = ("upload", "attach", "confirm", "use this file", "use file", "done")


def _click_upload_confirm_if_present(page: Page, file_locator: Locator, label: str) -> None:
    """
    Best-effort second step for two-stage upload widgets. Some ATS forms
    reveal a separate confirm/attach button after set_input_files() is
    called; this waits briefly for a possible re-render and clicks a
    confirm-shaped button scoped to the file input's container (never
    page-wide). No-op if nothing appears — single-step widgets are
    unaffected.
    """
    try:
        page.wait_for_timeout(500)
        container = file_locator.locator(
            "xpath=ancestor::*[self::form or self::fieldset or self::div][1]"
        )
        pattern = re.compile("|".join(_UPLOAD_CONFIRM_WORDS), re.IGNORECASE)
        confirm_button = container.get_by_role("button", name=pattern).first
        confirm_button.wait_for(state="visible", timeout=2000)
        confirm_button.click(timeout=2000)
        logger.info(f"[{label}] Clicked a follow-up upload-confirm button after file selection.")
    except (PlaywrightTimeoutError, PlaywrightError):
        pass


# --------------------------------------------------------------------------- #
# Autofill-trigger detection (hard block, not just a prompt instruction)
# --------------------------------------------------------------------------- #

# Phrases indicating an ATS's own "parse my resume and fill out the whole
# form" button, rather than a genuine data field or a plain resume upload
# input. Deliberately broad: a false positive (skipping an unusually
# worded field) is far cheaper than a false negative (letting the ATS's
# parser silently overwrite the whole form).
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
    """Dumps an element's outerHTML on failure so it's clear why it wasn't interactable."""
    try:
        html = locator.evaluate("el => el.outerHTML", timeout=1000)
        if html and len(html) > 800:
            html = html[:800] + "... [truncated]"
        logger.warning(f"[{label}] outerHTML at failure time: {html}")
    except PlaywrightError as e:
        logger.warning(f"[{label}] Could not retrieve outerHTML for debugging (element may not exist at all): {e}")


def _resilient_fill(locator, value: str, label: str) -> None:
    """
    Fill with a click-to-focus fallback. Some custom-styled inputs reject
    a direct fill() even though they're visible/attached — this is the
    "100% confidence but still times out" symptom, unrelated to the AI's
    confidence itself.
    """
    try:
        locator.fill(value, timeout=RESILIENT_TIMEOUT_MS)
        return
    except PlaywrightError as e:
        logger.info(f"[{label}] Direct fill failed ({e}); retrying via click-to-focus fallback.")

    locator.click(timeout=RESILIENT_TIMEOUT_MS)
    locator.fill(value, timeout=RESILIENT_TIMEOUT_MS)


_BOOLEAN_TRUE_STRINGS = {"true", "yes", "1"}
_BOOLEAN_FALSE_STRINGS = {"false", "no", "0"}


def _resilient_select(locator, value: str, label: str) -> None:
    """
    select_option() with a fuzzy fallback and a click-to-focus fallback.

    The fuzzy fallback handles the LLM returning a raw config value (e.g.
    Python bool True) instead of the dropdown's actual option text
    ("Yes") — select_option() can't fuzzy-match on its own, so this is a
    code-level safety net rather than relying solely on prompt instructions.
    """
    try:
        try:
            locator.select_option(label=value, timeout=ELEMENT_TIMEOUT_MS)
        except PlaywrightError:
            locator.select_option(value=value, timeout=ELEMENT_TIMEOUT_MS)
        return
    except PlaywrightError as e:
        logger.info(f"[{label}] Direct select_option failed ({e}); trying fuzzy option match.")

    try:
        option_texts = locator.evaluate(
            "el => Array.from(el.options).map(o => o.textContent.trim())"
        ) or []
    except PlaywrightError:
        option_texts = []

    # Boolean-ish values ("True"/"yes"/"1") don't resemble "Yes"/"No"
    # closely enough for fuzzy matching below to reliably catch.
    normalized_value = value
    lowered = value.strip().lower()
    if lowered in _BOOLEAN_TRUE_STRINGS:
        yes_option = next((o for o in option_texts if o.strip().lower() == "yes"), None)
        if yes_option:
            normalized_value = yes_option
    elif lowered in _BOOLEAN_FALSE_STRINGS:
        no_option = next((o for o in option_texts if o.strip().lower() == "no"), None)
        if no_option:
            normalized_value = no_option

    option_lookup = {text: text for text in option_texts}
    matched_text, strategy = _find_best_option_match(normalized_value, option_lookup)
    if matched_text:
        logger.info(f"[{label}] Fuzzy-matched select value {value!r} -> {matched_text!r} via {strategy}.")
        try:
            locator.select_option(label=matched_text, timeout=RESILIENT_TIMEOUT_MS)
            return
        except PlaywrightError:
            pass

    logger.info(f"[{label}] No fuzzy match found for {value!r} among options {option_texts}; "
                f"retrying via click-to-focus fallback.")
    locator.click(timeout=RESILIENT_TIMEOUT_MS)
    try:
        locator.select_option(label=value, timeout=RESILIENT_TIMEOUT_MS)
    except PlaywrightError:
        locator.select_option(value=value, timeout=RESILIENT_TIMEOUT_MS)


# --------------------------------------------------------------------------- #
# radio_group injection (no single element -- resolved via option_selectors)
# --------------------------------------------------------------------------- #

def _escape_attr_value(value: str) -> str:
    """Escapes a value for safe use inside a quoted attribute selector like [name="value"]."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


# Fast per-attempt budget while probing candidates across multiple
# frames/strategies, so up to 5 strategies x several iframes doesn't
# multiply into a very slow field. RESILIENT_TIMEOUT_MS still applies to
# the actual fill/select sequence once an element is resolved.
FRAME_PROBE_TIMEOUT_MS = 1500


def _search_contexts(page: Page) -> list[Page | Frame]:
    """Main frame first (fastest to fail out of), then every iframe — some
    ATS platforms embed the actual form in an iframe."""
    return [page] + [f for f in page.frames if f != page.main_frame]


def _text_proximity_locator(context: Page | Frame, text: str) -> Locator:
    """
    Fallback for sites with no real <label> association at all (Ashby is
    a known example — visually-styled divs above each input, no
    programmatic link). Finds the element containing the exact label
    text, then takes the nearest input/select/textarea that follows it in
    DOM order. A real heuristic, not a guaranteed relationship — hence
    its position late in the resolution cascade below.
    """
    text_node = context.get_by_text(text, exact=True).first
    text_node.wait_for(state="visible", timeout=FRAME_PROBE_TIMEOUT_MS)
    return text_node.locator(
        "xpath=following::input[1] | following::select[1] | following::textarea[1]"
    )


def _resolve_locator(
    page: Page,
    label: str | None = None,
    placeholder: str | None = None,
    name: str | None = None,
    css_selector: str | None = None,
    timeout_ms: int = RESILIENT_TIMEOUT_MS,
    require_visible: bool = True,
) -> tuple[Locator | None, str]:
    """
    Resolves an element via a cascade tried in order of stability (not
    availability), searched across the main page and every iframe at each
    step. This is the fix for frameworks that regenerate ephemeral IDs on
    re-render (e.g. Radix UI, used by Ashby) and for forms embedded in an
    iframe.

    Strategy order, most to least stable:
      1. get_by_label(label, exact=True) — resolved fresh against the
         current DOM. Exact match only, since fuzzy substring matching
         risks matching unrelated page chrome sharing a common word.
      2. get_by_placeholder(placeholder, exact=True)
      3. [name="..."] — part of the form's submission contract, rarely
         framework-generated or changed on re-render, unlike id.
      4. Text-proximity fallback (_text_proximity_locator) — for sites
         with no real <label> association at all.
      5. The raw scrape-time selector_hint (sanitized) — last resort,
         since it's the most likely to have gone stale.

    require_visible=False accepts an element merely attached to the DOM.
    Needed for file inputs: custom-styled upload widgets almost always
    hide the real <input type="file"> via CSS while keeping it fully
    functional, and set_input_files() (unlike fill()) doesn't need
    visibility.

    Returns (locator_or_None, strategy_description) for logging/debugging.
    """
    wait_state = "visible" if require_visible else "attached"
    contexts = _search_contexts(page)

    def _try_across_contexts(make_locator, per_attempt_timeout: int) -> Locator | None:
        for ctx in contexts:
            try:
                locator = make_locator(ctx).first
                locator.wait_for(state=wait_state, timeout=per_attempt_timeout)
                return locator
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
        return None

    tried: list[str] = []

    if label:
        tried.append(f"label={label!r}")
        found = _try_across_contexts(
            lambda ctx: ctx.get_by_label(label, exact=True), FRAME_PROBE_TIMEOUT_MS
        )
        if found:
            return found, tried[-1]

    if placeholder:
        tried.append(f"placeholder={placeholder!r}")
        found = _try_across_contexts(
            lambda ctx: ctx.get_by_placeholder(placeholder, exact=True), FRAME_PROBE_TIMEOUT_MS
        )
        if found:
            return found, tried[-1]

    if name:
        tried.append(f'name="{name}"')
        found = _try_across_contexts(
            lambda ctx: ctx.locator(f'[name="{_escape_attr_value(name)}"]'), FRAME_PROBE_TIMEOUT_MS
        )
        if found:
            return found, tried[-1]

    if label:
        tried.append(f"text-proximity={label!r}")
        found = _try_across_contexts(
            lambda ctx: _text_proximity_locator(ctx, label), FRAME_PROBE_TIMEOUT_MS
        )
        if found:
            return found, tried[-1]

    if css_selector:
        tried.append(f"selector={css_selector!r}")
        # Last resort gets the full timeout budget, searched across frames too.
        found = _try_across_contexts(
            lambda ctx: ctx.locator(_sanitize_selector(css_selector)), timeout_ms
        )
        if found:
            return found, tried[-1]

    tried_desc = " -> ".join(tried) if tried else "no identifying info available at all"
    return None, f"none matched across {len(contexts)} context(s) (tried: {tried_desc})"


# The "decline to answer" family of EEO answers shows up with very
# different exact phrasing across ATS platforms but always means the same
# thing. Generic fuzzy matching often doesn't score these close enough to
# pass a sane similarity threshold, so it gets its own equivalence class.
_DECLINE_TO_ANSWER_MARKERS = (
    "decline to answer", "decline to self-identify", "decline to self identify",
    "decline to state", "prefer not to answer", "prefer not to say",
    "prefer not to disclose", "i don't wish to answer", "i do not wish to answer",
    "choose not to disclose", "rather not say", "not wish to answer",
    "do not wish to disclose",
)


def _is_decline_to_answer_phrase(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    return any(marker in normalized for marker in _DECLINE_TO_ANSWER_MARKERS)


def _find_best_option_match(value: str, option_selectors: dict[str, str]) -> tuple[str | None, str]:
    """
    Resolves which scraped option the AI's chosen value refers to, via
    progressively fuzzier matching: exact match, case/whitespace-
    normalized match, decline-to-answer synonym match, token-subset match
    (catches a qualifying word inserted mid-phrase, e.g. "not a veteran"
    vs "not a protected veteran"), then character-level similarity as a
    last resort.

    Returns (matched_option_text_or_None, match_strategy_description).
    """
    if value in option_selectors:
        return value, "exact match"

    normalized_value = " ".join(value.strip().lower().split())
    for option_text in option_selectors:
        if " ".join(option_text.strip().lower().split()) == normalized_value:
            return option_text, "case/whitespace-normalized match"

    if _is_decline_to_answer_phrase(normalized_value):
        for option_text in option_selectors:
            if _is_decline_to_answer_phrase(option_text):
                return option_text, "decline-to-answer synonym match"

    # Regex word tokenization (not plain .split()) so "Yes," and "Yes"
    # aren't treated as different tokens purely due to a trailing comma.
    def _word_tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    value_tokens = _word_tokens(normalized_value)
    best_subset_match: str | None = None
    if value_tokens:
        for option_text in option_selectors:
            option_tokens = _word_tokens(option_text)
            if value_tokens <= option_tokens or option_tokens <= value_tokens:
                # Among multiple subset matches, prefer the least "over-qualified" one.
                if best_subset_match is None or abs(len(option_text) - len(value)) < abs(len(best_subset_match) - len(value)):
                    best_subset_match = option_text
    if best_subset_match:
        return best_subset_match, "token-subset match"

    best_ratio = 0.0
    best_option: str | None = None
    for option_text in option_selectors:
        ratio = difflib.SequenceMatcher(None, normalized_value, option_text.strip().lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_option = option_text
    if best_option and best_ratio >= 0.6:
        return best_option, f"fuzzy similarity match ({best_ratio:.0%})"

    return None, "no match found via any strategy"


def _inject_radio_group(page: Page, record: dict[str, Any], result: FieldResult) -> FieldResult:
    """
    Handles field_type == "radio_group". There is no single element to
    locate (jobScraper sets selector_hint to None for groups) — instead,
    option_selectors maps each option's text to that specific radio's
    real selector, and we click whichever matches the AI's chosen value.

    Unlike _inject_single_field, the scrape-time selector is tried FIRST:
    radio option text is often short and generic ("Yes"/"No"), so a fuzzy
    label match risks clicking the wrong element elsewhere on the page.
    Only if the direct selector has gone stale does this fall back to an
    exact (non-fuzzy) label match.
    """
    label = result.label
    confidence = result.confidence
    value = result.value

    if value is None or value == "":
        return result

    value = str(value)
    option_selectors: dict[str, str] = record.get("option_selectors", {})

    matched_option_text, match_strategy = _find_best_option_match(value, option_selectors)
    chosen_selector = option_selectors.get(matched_option_text) if matched_option_text else None

    if match_strategy not in ("exact match", "case/whitespace-normalized match"):
        logger.info(f"[{label}] Matched {value!r} -> {matched_option_text!r} via {match_strategy}.")

    if not chosen_selector:
        msg = f"AI chose {value!r}, which isn't one of the scraped options: {list(option_selectors.keys())}"
        logger.warning(f"[{label}] {msg}")
        result.status = "error"
        result.error_message = msg
        return result

    locator = None
    strategy = "none"
    contexts = _search_contexts(page)
    for candidate_strategy, make_locator in (
        (f"selector={chosen_selector!r}", lambda ctx: ctx.locator(_sanitize_selector(chosen_selector))),
        (f"exact label={matched_option_text!r}", lambda ctx: ctx.get_by_label(matched_option_text, exact=True)),
    ):
        for ctx in contexts:
            try:
                candidate = make_locator(ctx).first
                candidate.wait_for(state="visible", timeout=RESILIENT_TIMEOUT_MS)
                locator = candidate
                strategy = candidate_strategy
                break
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
        if locator is not None:
            break

    if locator is None:
        msg = f"Could not locate radio option {value!r} — tried selector and exact label match, neither resolved"
        logger.warning(f"[{label}] {msg}")
        result.status = "error"
        result.error_message = msg
        return result

    result.selector = strategy

    try:
        try:
            locator.scroll_into_view_if_needed(timeout=RESILIENT_TIMEOUT_MS)
        except PlaywrightError:
            pass
        locator.wait_for(state="attached", timeout=RESILIENT_TIMEOUT_MS)

        # check() is correct for native inputs and role="checkbox"; for
        # role="radio" custom buttons (no native checked state at all), a
        # plain click() is the right activation — this handles both kinds
        # without needing to know in advance which one a field is.
        try:
            locator.check(timeout=RESILIENT_TIMEOUT_MS)
        except PlaywrightError:
            locator.click(timeout=RESILIENT_TIMEOUT_MS)

        result.status = "filled"
        highlight_field(locator, confidence, filled=True)

    except PlaywrightTimeoutError:
        msg = f"Timed out selecting radio option {value!r} within {RESILIENT_TIMEOUT_MS}ms (resolved via {strategy})"
        logger.warning(f"[{label}] {msg}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = msg
        highlight_field(locator, confidence, filled=False)

    except PlaywrightError as e:
        logger.warning(f"[{label}] Playwright error selecting radio option: {e}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = str(e)
        highlight_field(locator, confidence, filled=False)

    return result


def _inject_single_field(page: Page, record: dict[str, Any]) -> FieldResult:
    """
    Attempts to fill one field according to its declared type. Returns a
    FieldResult regardless of success/failure — callers never need to
    catch exceptions from this directly.
    """
    raw_selector = record.get("selector")
    field_type = record.get("field_type", "text")
    value = record.get("value")
    confidence = float(record.get("confidence", 0.0))
    label = record.get("label") or raw_selector or "UNKNOWN_FIELD"
    placeholder = record.get("placeholder")
    name_attr = record.get("name")

    result = FieldResult(
        label=label,
        selector=raw_selector or "(radio_group -- see option_selectors)",
        value=value,
        confidence=confidence,
        status="skipped",
    )

    # Hard block, not just a prompt instruction: never interact with an
    # ATS's own "autofill from resume" trigger, since it hands the entire
    # form over to the site's own parser and overwrites everything we
    # just mapped from config.json. EXEMPTION: field_type == "file" is
    # never blocked, even if its label mentions "autofill" — some ATS
    # platforms phrase the one legitimate resume upload as "Resume —
    # upload to autofill this application". set_input_files() on a
    # genuine file input can't trigger a JS-driven parse/autofill action
    # the way clicking a styled div/button can.
    if _is_autofill_trigger(label) and field_type != FieldType.FILE:
        logger.warning(
            f"[{label}] Refusing to interact -- this looks like an ATS "
            f"auto-parse-from-resume trigger, not a genuine data field. Skipping."
        )
        result.status = "skipped"
        result.error_message = "Blocked: appears to be an autofill-from-resume trigger, not a data field"
        return result

    # radio_group has no single element by design — jobScraper sets
    # selector_hint to None since no one element represents the group.
    if field_type == FieldType.RADIO_GROUP:
        return _inject_radio_group(page, record, result)

    # Resolve via the label -> placeholder -> name -> scrape-time-selector
    # cascade rather than trusting raw_selector alone, since a
    # scrape-time id can go stale by injection time (e.g. Radix UI
    # regenerating ids on re-render). Resolved before the value check so
    # empty-value fields still get found and highlighted red for review.
    locator, strategy = _resolve_locator(
        page,
        label=label if label != raw_selector else None,
        placeholder=placeholder,
        name=name_attr,
        css_selector=raw_selector,
        # File inputs are commonly hidden by design; set_input_files()
        # doesn't need visibility the way fill()/click() do.
        require_visible=(field_type != FieldType.FILE),
    )
    result.selector = strategy

    if locator is None:
        msg = f"Could not locate element via any strategy ({strategy})"
        logger.warning(f"[{label}] {msg}")
        result.status = "error"
        result.error_message = msg
        return result

    if value is None or value == "":
        highlight_field(locator, confidence, filled=False)
        return result

    try:
        # Scroll into view, then confirm still attached, before
        # interacting. Uses RESILIENT_TIMEOUT_MS since these are fields
        # the AI already matched with real confidence.
        try:
            locator.scroll_into_view_if_needed(timeout=RESILIENT_TIMEOUT_MS)
        except PlaywrightError:
            pass
        locator.wait_for(state="attached", timeout=RESILIENT_TIMEOUT_MS)

        if field_type in TEXT_LIKE_TYPES or field_type == FieldType.DATE:
            _resilient_fill(locator, str(value), label)

        elif field_type == FieldType.FILE:
            file_path = Path(str(value)).expanduser()
            if not file_path.is_file():
                raise FileNotFoundError(f"Resume file not found at: {file_path}")
            locator.set_input_files(str(file_path), timeout=RESILIENT_TIMEOUT_MS)
            _click_upload_confirm_if_present(page, locator, label)

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
        highlight_field(locator, confidence, filled=True)

    except FileNotFoundError as e:
        logger.warning(f"[{label}] {e}")
        result.status = "error"
        result.error_message = str(e)
        highlight_field(locator, confidence, filled=False)

    except PlaywrightTimeoutError:
        msg = f"Timed out waiting for element (resolved via {strategy}, but not interactable) within {RESILIENT_TIMEOUT_MS}ms"
        logger.warning(f"[{label}] {msg}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = msg
        highlight_field(locator, confidence, filled=False)

    except PlaywrightError as e:
        logger.warning(f"[{label}] Playwright error: {e}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = str(e)
        highlight_field(locator, confidence, filled=False)

    except Exception as e:  # noqa: BLE001 - defensive catch-all; log and continue the batch
        logger.error(f"[{label}] Unexpected error: {e}")
        _log_outer_html_for_debug(locator, label)
        result.status = "error"
        result.error_message = str(e)
        highlight_field(locator, confidence, filled=False)

    return result


def inject_matched_fields(page: Page, matched_fields: list[dict[str, Any]]) -> InjectionReport:
    """
    Main entry point for Phase 4. Iterates every field the AI matching
    engine mapped, fills it on the live page, and applies a visual
    confidence border. A single field's failure never stops the batch.

    Args:
        page: An active Playwright Page already navigated to the application form.
        matched_fields: The list of field-mapping dicts produced by Phase 3.

    Returns:
        InjectionReport summarizing every field's outcome.
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

        # Hand off to Review Gate (Phase 5): page.pause() for a headed
        # manual check, or a CLI y/n prompt before submit.
        page.pause()
        browser.close()