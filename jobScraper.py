"""
job_agent_scraper.py

Phase 2: Extraction Layer for the local job-application agent.

main.py owns the browser/page lifecycle and hands this module an
already-navigated `page` via scrape_page(page). This module never launches,
navigates, or closes a browser itself.

Responsibilities:
    - Recursively scan the main page AND all nested iframes (Workday, Taleo,
      Greenhouse, Lever, etc. commonly embed forms in cross-origin iframes).
    - Extract standard form fields (input, textarea, select) AND
      "custom" JS-framework dropdowns (div/button elements styled as
      selects, using role="combobox"/"listbox" or common class patterns).
    - Bundle everything into a clean, structured dict ready to be handed
      to the LLM matching layer in Phase 3.

No LLM/Ollama code lives here — this module's only job is DOM -> structured data.
"""

import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

from playwright.sync_api import (
    Page,
    Frame,
    Error as PlaywrightError,
)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class FormField:
    """A single scraped form field, normalized for downstream LLM matching."""
    field_type: str            # "text", "textarea", "select", "checkbox", "radio_group", "custom_dropdown", etc.
    name: Optional[str] = None
    element_id: Optional[str] = None
    label: Optional[str] = None        # text from an associated <label>
    placeholder: Optional[str] = None
    aria_label: Optional[str] = None
    required: bool = False
    options: list = field(default_factory=list)  # for select / custom dropdowns / radio_group
    frame_url: Optional[str] = None    # which frame (main page or iframe src) this came from
    selector_hint: Optional[str] = None  # REAL CSS/attribute selector Playwright uses to locate this element
    context_key: Optional[str] = None    # human-readable identifier sent to the LLM -- NEVER used as a CSS selector
    option_selectors: dict = field(default_factory=dict)  # radio_group only: option label -> that radio's real selector

    def best_context(self) -> str:
        """Return the most useful human-readable description of what this field asks."""
        return self.label or self.placeholder or self.aria_label or self.name or self.element_id or "UNKNOWN_FIELD"


# Phrases that give the LLM essentially zero signal about WHAT is being
# asked -- e.g. a date picker's generic placeholder text rather than the
# actual question ("graduation date" vs "start date" vs "birth date").
# When the best label we've found matches one of these, we widen the
# search rather than accepting it.
_VAGUE_LABEL_SUBSTRINGS = (
    "pick date", "pick a date", "select date", "select a date",
    "select option", "select an option", "select one", "choose one",
    "choose an option", "select...", "pick...",
)


def _is_vague_label(text: Optional[str]) -> bool:
    if not text:
        return True
    normalized = " ".join(text.strip().lower().split()).rstrip(".…")
    if len(normalized) < 3:
        return True
    if normalized in ("select", "choose", "date", "pick", "option"):
        return True
    return any(marker in normalized for marker in _VAGUE_LABEL_SUBSTRINGS)


# Date labels that are correct and complete for a ONE-OFF date field, but
# ambiguous when the same label repeats identically across multiple
# sections of a form (an education entry vs. each work_history entry all
# asking for a "Start Date"). _is_vague_label deliberately does NOT flag
# these -- they're not incomplete/uninformative in isolation -- but they
# need a section prefix to disambiguate which entry they belong to.
_GENERIC_DATE_LABELS = ("start date", "end date", "from", "to", "from date", "to date")


def _is_generic_date_label(text: Optional[str]) -> bool:
    if not text:
        return True
    normalized = " ".join(text.strip().lower().split())
    return normalized in _GENERIC_DATE_LABELS


def _slugify(text: str, max_len: int = 80) -> str:
    """
    Turns human-readable label text into a stable, readable identifier the
    LLM can actually reason about (e.g. "graduation_date"), instead of an
    ugly nth-of-type CSS path or an opaque framework-generated ID.

    Truncates at a WORD boundary, never mid-word. The old hard cut at 60
    chars produced field_ids like "...if_so_expecte" -- silently chopping
    off exactly the word ("expected") that disambiguated the field. This
    field_id is never used as a CSS selector (see selector_hint for that),
    so there's no strict length requirement forcing a hard cut in the
    first place.
    """
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    if not text:
        return "field"
    if len(text) <= max_len:
        return text

    truncated = text[:max_len]
    last_underscore = truncated.rfind("_")
    # Back off to the last complete word, but not if that throws away more
    # than half the budget -- better a slightly-too-long id than one that's
    # lost most of its content.
    if last_underscore > max_len * 0.5:
        truncated = truncated[:last_underscore]
    return truncated or "field"


# --------------------------------------------------------------------------
# Scraper
# --------------------------------------------------------------------------

class JobFormScraper:
    def __init__(self, nav_timeout_ms: int = 30000):
        self.nav_timeout_ms = nav_timeout_ms

    # ---- public entrypoint (shared-page pipeline) --------------------------

    def scrape_page(self, page: Page) -> dict:
        """
        Scrape an ALREADY-NAVIGATED Playwright page (and its frames).
        Does not launch, navigate, or close a browser — main.py owns that
        lifecycle now. Call this after page.goto() has already run.
        """
        # Give dynamically-rendered (React/Angular) forms a moment to mount.
        page.wait_for_timeout(1500)

        all_fields: list[FormField] = []

        # Scrape the main page, then recurse into every frame.
        all_fields.extend(self._scrape_frame(page.main_frame))

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                all_fields.extend(self._scrape_frame(frame))
            except PlaywrightError as e:
                # Some frames (ads, trackers, analytics) are inaccessible or irrelevant.
                # We log and move on rather than failing the whole scrape.
                print(f"[warn] Could not scrape frame {frame.url}: {e}", file=sys.stderr)

        return self._bundle(page.url, all_fields)

    # ---- internals -----------------------------------------------------

    def _scrape_frame(self, frame: Frame) -> list[FormField]:
        """Scrape standard inputs/selects/textareas plus custom dropdowns from one frame."""
        fields: list[FormField] = []
        frame_url = frame.url or "about:blank"

        fields.extend(self._scrape_standard_inputs(frame, frame_url))
        fields.extend(self._scrape_radio_groups(frame, frame_url))
        fields.extend(self._scrape_selects(frame, frame_url))
        fields.extend(self._scrape_custom_dropdowns(frame, frame_url))

        return fields

    def _immediate_label(self, frame: Frame, element_handle) -> Optional[str]:
        """
        Strategies 1-3 only, with NO vague-text filtering: <label for="id">,
        parent <label>, aria-labelledby. Returns whatever it finds as-is,
        even if short (e.g. "No", "Yes") -- callers that want a genuine
        FIELD LABEL should use _label_for() instead, which adds vague-text
        detection and broader-context fallback on top of this. This
        unfiltered version exists specifically for resolving an individual
        radio's own OPTION text, where short, complete answers like "No"
        are correct and must NOT be treated as "too vague" and replaced
        with something else.
        """
        candidate: Optional[str] = None
        try:
            el_id = element_handle.get_attribute("id")
            if el_id:
                label_el = frame.query_selector(f'label[for="{el_id}"]')
                if label_el:
                    text = label_el.inner_text().strip()
                    if text:
                        candidate = text

            if not candidate:
                # Parent <label> wrapping the field (common in custom form libraries)
                parent_label = element_handle.evaluate_handle("el => el.closest('label')")
                if parent_label:
                    el = parent_label.as_element()
                    text = el.inner_text().strip() if el else None
                    if text:
                        candidate = text

            if not candidate:
                labelledby = element_handle.get_attribute("aria-labelledby")
                if labelledby:
                    # aria-labelledby can reference multiple space-separated ids
                    texts = []
                    for ref_id in labelledby.split():
                        ref_el = frame.query_selector(f'#{ref_id}')
                        if ref_el:
                            t = ref_el.inner_text().strip()
                            if t:
                                texts.append(t)
                    if texts:
                        candidate = " ".join(texts)
        except PlaywrightError:
            pass
        return candidate

    def _label_for(self, frame: Frame, element_handle) -> Optional[str]:
        """
        Resolve the best human-readable label for an element. Tries the
        standard semantic strategies first, but -- unlike before -- doesn't
        stop at the first non-empty result. If what it found is missing OR
        too vague to tell the LLM what's actually being asked (e.g. a date
        picker's placeholder just says "Pick date...", giving no signal
        about WHICH date), it widens the search via _find_broader_context.

        Strategies, in order:
          1. <label for="id">
          2. Parent <label> wrapping the input
          3. aria-labelledby
          4. (only if 1-3 found nothing, or found something vague) nearest
             ancestor <fieldset>'s <legend>, or the nearest ancestor's own
             non-input text -- common in custom form libraries that render
             a question visually above/around a bare input without ever
             using a semantic <label> element.

        NOTE: this vague-check-and-broaden behavior is meant for resolving
        FIELD labels/questions. Do NOT use this for an individual radio's
        own option text (e.g. "No") -- short, valid answers get wrongly
        flagged as vague and replaced with broader (wrong) context. Use
        _immediate_label() for that instead; see _scrape_radio_groups.
        """
        candidate = self._immediate_label(frame, element_handle)

        if candidate and not _is_vague_label(candidate):
            return candidate

        # Nothing found, or what we found is too vague to be useful --
        # widen the search rather than handing the LLM "Pick date..." and
        # letting it guess which date field this actually is.
        broader = self._find_broader_context(element_handle)
        if broader:
            return broader

        return candidate  # possibly None, possibly still-vague -- better than nothing

    def _find_broader_context(self, element_handle) -> Optional[str]:
        """
        Fallback for when the immediate label/placeholder is missing or too
        vague. Walks up the DOM looking for:
          1. The nearest ancestor <fieldset>'s <legend> text -- the
             standard semantic pattern for grouped/radio fields, and
             usually the actual question for a radio group.
          2. The nearest ancestor with a non-input child (or its own direct
             text) that reads like a real question/heading -- common in
             custom form libraries (Greenhouse, Ashby, Workday) that render
             a question visually above an input without a semantic <label>
             at all.
        """
        try:
            return element_handle.evaluate("""
                el => {
                    function cleanText(node) {
                        if (!node) return '';
                        return (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ');
                    }

                    const fs = el.closest('fieldset');
                    if (fs) {
                        const legend = fs.querySelector('legend');
                        const t = cleanText(legend);
                        if (t && t.length > 2 && t.length < 300) return t;
                    }

                    let node = el;
                    for (let depth = 0; depth < 5 && node; depth++) {
                        node = node.parentElement;
                        if (!node) break;
                        for (const child of node.children) {
                            if (child === el) continue;
                            if (['INPUT','SELECT','TEXTAREA','BUTTON','OPTION'].includes(child.tagName)) continue;
                            const t = cleanText(child);
                            if (t && t.length > 2 && t.length < 300) return t;
                        }
                        const ownText = Array.from(node.childNodes)
                            .filter(n => n.nodeType === Node.TEXT_NODE)
                            .map(n => n.textContent.trim())
                            .join(' ').trim();
                        if (ownText && ownText.length > 2 && ownText.length < 300) return ownText;
                    }
                    return null;
                }
            """)
        except PlaywrightError:
            return None

    def _find_section_heading(self, element_handle) -> Optional[str]:
        """
        Walks further up the DOM than _find_broader_context, specifically
        looking for the nearest PRECEDING section heading (h1-h6, or a
        <legend> acting as a section divider -- e.g. "Education", "Work
        Experience"). Used to disambiguate generic per-entry fields like
        "Start Date"/"End Date" that repeat identically across multiple
        form sections and would otherwise be indistinguishable to the LLM
        -- this is what causes a graduation date to get matched against a
        work-history start date instead.
        """
        try:
            return element_handle.evaluate("""
                el => {
                    function cleanText(node) {
                        if (!node) return '';
                        return (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ');
                    }
                    let node = el;
                    for (let depth = 0; depth < 10 && node; depth++) {
                        let sib = node.previousElementSibling;
                        while (sib) {
                            if (/^H[1-6]$/.test(sib.tagName)) {
                                const t = cleanText(sib);
                                if (t && t.length > 1 && t.length < 60) return t;
                            }
                            if (sib.tagName === 'LEGEND') {
                                const t = cleanText(sib);
                                if (t && t.length > 1 && t.length < 60) return t;
                            }
                            sib = sib.previousElementSibling;
                        }
                        node = node.parentElement;
                        if (!node) break;
                    }
                    return null;
                }
            """)
        except PlaywrightError:
            return None

    def _scrape_standard_inputs(self, frame: Frame, frame_url: str) -> list[FormField]:
        fields = []
        try:
            elements = frame.query_selector_all("input, textarea")
        except PlaywrightError:
            return fields

        for el in elements:
            try:
                input_type = (el.get_attribute("type") or "text").lower()

                # Skip noise: hidden fields, submit/button inputs, honeypots.
                # NOTE: "file" used to be skipped here too, which silently
                # dropped resume/CV upload fields before they ever reached
                # the matcher. Kept out of this skip list intentionally.
                # "radio" is now skipped HERE deliberately -- individual
                # radio inputs get grouped into one logical field by
                # _scrape_radio_groups() instead of being scraped as
                # isolated, meaningless single fields.
                if input_type in ("hidden", "submit", "button", "image", "radio"):
                    continue
                if not el.is_visible():
                    continue

                tag = el.evaluate("el => el.tagName.toLowerCase()")
                placeholder = el.get_attribute("placeholder")
                aria_label = el.get_attribute("aria-label")
                name_attr = el.get_attribute("name")
                id_attr = el.get_attribute("id")
                label_text = self._label_for(frame, el)

                field_type = "textarea" if tag == "textarea" else input_type

                # Some ATS platforms build date pickers on a plain
                # type="text" input rather than a native type="date" input
                # (native ones are already caught correctly above via
                # input_type). Reclassify those based on surrounding hints
                # so injection.py knows to treat them as dates rather than
                # plain text.
                if field_type == "text":
                    date_hints = " ".join(filter(None, [
                        aria_label, placeholder, name_attr, id_attr, label_text,
                    ])).lower()
                    if "date" in date_hints or "dob" in date_hints:
                        field_type = "date"

                # Repeating form sections (an education entry's start/end
                # dates vs. EACH work_history entry's start/end dates) very
                # often share the exact same generic label ("Start Date"),
                # with nothing distinguishing which entry a given field
                # belongs to -- this is what causes a graduation date to
                # get matched against a work_history start_date instead.
                # Prefix with the nearest section heading when the label
                # is this generic, so the LLM sees "Education — Start
                # Date" rather than an ambiguous "Start Date" repeated
                # identically across every section.
                if field_type == "date" and _is_generic_date_label(label_text):
                    section = self._find_section_heading(el)
                    if section:
                        label_text = f"{section} — {label_text}" if label_text else section

                context_source = label_text or placeholder or aria_label or name_attr or id_attr or "field"

                fields.append(FormField(
                    field_type=field_type,
                    name=name_attr,
                    element_id=id_attr,
                    label=label_text,
                    placeholder=placeholder,
                    aria_label=aria_label,
                    required=el.get_attribute("required") is not None
                              or el.get_attribute("aria-required") == "true",
                    frame_url=frame_url,
                    selector_hint=self._build_selector_hint(el),
                    context_key=_slugify(context_source),
                ))
            except PlaywrightError:
                continue  # Element detached / stale — skip rather than crash the run.

        return fields

    def _scrape_radio_groups(self, frame: Frame, frame_url: str) -> list[FormField]:
        """
        Groups <input type="radio"> elements sharing the same `name` into
        ONE logical FormField (like a select), rather than emitting each
        radio as an isolated, meaningless single field the LLM has no way
        to reason about collectively (e.g. seeing just "Yes, OPT" alone,
        with no idea "No" and "Yes, H1B" are the other choices).

        Radios without a `name` attribute can't be reliably grouped (the
        browser itself relies on `name` to know which radios are mutually
        exclusive), so those are skipped rather than guessed at.
        """
        fields: list[FormField] = []
        try:
            radios = frame.query_selector_all('input[type="radio"]')
        except PlaywrightError:
            return fields

        groups: dict[str, list] = {}
        for el in radios:
            try:
                if not el.is_visible():
                    continue
                name = el.get_attribute("name")
                if not name:
                    continue
                groups.setdefault(name, []).append(el)
            except PlaywrightError:
                continue

        for name, radio_elements in groups.items():
            options: list[str] = []
            option_selectors: dict[str, str] = {}
            required = False

            for el in radio_elements:
                try:
                    required = required or el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true"

                    # Each radio's own label is the OPTION text ("Yes, OPT",
                    # or just "No") -- use the unfiltered _immediate_label,
                    # NOT _label_for's vague-check-and-broaden behavior.
                    # That behavior is meant for field labels/questions;
                    # applied to a short-but-valid answer like "No" it
                    # wrongly treats it as "too vague" and replaces it with
                    # the group's own question text instead.
                    option_text = self._immediate_label(frame, el) or (el.get_attribute("value") or "").strip()
                    if not option_text:
                        continue

                    option_hint = self._build_selector_hint(el)
                    if option_text not in option_selectors:
                        options.append(option_text)
                        option_selectors[option_text] = option_hint
                except PlaywrightError:
                    continue

            if not options:
                continue

            # Resolve the GROUP's shared question (e.g. "Are you authorized
            # to work without sponsorship?") -- done ONCE, from all radios
            # together, not per-radio. Searching from a single radio just
            # re-finds that radio's own paired option label ("Authorized,
            # no CPT/OPT needed") and wrongly treats it as the question.
            group_label = self._find_group_question(radio_elements, options)

            context_source = group_label or name
            fields.append(FormField(
                field_type="radio_group",
                name=name,
                label=group_label,
                required=required,
                options=options,
                frame_url=frame_url,
                selector_hint=None,  # no single element represents the group -- see option_selectors
                context_key=_slugify(context_source),
                option_selectors=option_selectors,
            ))

        return fields

    def _find_group_question(self, radio_elements: list, option_texts: list[str]) -> Optional[str]:
        """
        Resolves the shared question for a group of radios, as distinct
        from any individual radio's own option label. The naive approach
        (search from one radio) just re-finds that radio's own paired
        <label> ("Yes"/"No"/"Authorized, no CPT/OPT needed") since that's
        the nearest plausible text to any single radio -- it has no way to
        know "text near this one radio" and "the question for the whole
        group" are different things.

        Instead: find the nearest DOM ancestor common to EVERY radio in
        the group, then search for a <fieldset><legend> or heading-like
        text from THAT shared point rather than from any single radio.
        As a second safety net, any candidate that exactly matches one of
        the group's own option texts is rejected outright -- a real
        question should never be identical to one of its own answers.
        """
        if not radio_elements:
            return None

        try:
            first = radio_elements[0]
            others = radio_elements[1:]
            common_ancestor_handle = first.evaluate_handle(
                """(el, otherEls) => {
                    function ancestorChain(node) {
                        const chain = [];
                        let n = node;
                        while (n) { chain.push(n); n = n.parentElement; }
                        return chain;
                    }
                    let candidates = ancestorChain(el);
                    for (const other of otherEls) {
                        const otherSet = new Set(ancestorChain(other));
                        candidates = candidates.filter(a => otherSet.has(a));
                    }
                    return candidates.length ? candidates[0] : el;
                }""",
                others,
            )
            anchor = common_ancestor_handle.as_element() or first

            candidate = anchor.evaluate("""
                el => {
                    function cleanText(node) {
                        if (!node) return '';
                        return (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ');
                    }

                    const fs = el.closest('fieldset');
                    if (fs) {
                        const legend = fs.querySelector('legend');
                        const t = cleanText(legend);
                        if (t && t.length > 2 && t.length < 300) return t;
                    }

                    let node = el;
                    for (let depth = 0; depth < 5 && node; depth++) {
                        node = node.parentElement;
                        if (!node) break;
                        for (const child of node.children) {
                            // Skip form controls AND their paired labels --
                            // those are per-option text, not the group
                            // question.
                            if (['INPUT','SELECT','TEXTAREA','BUTTON','OPTION','LABEL'].includes(child.tagName)) continue;
                            const t = cleanText(child);
                            if (t && t.length > 2 && t.length < 300) return t;
                        }
                        const ownText = Array.from(node.childNodes)
                            .filter(n => n.nodeType === Node.TEXT_NODE)
                            .map(n => n.textContent.trim())
                            .join(' ').trim();
                        if (ownText && ownText.length > 2 && ownText.length < 300) return ownText;
                    }
                    return null;
                }
            """)
        except PlaywrightError:
            return None

        if not candidate or _is_vague_label(candidate):
            return None

        # Reject if it's just one of the group's own options in disguise.
        normalized_options = {" ".join(t.strip().lower().split()) for t in option_texts}
        if " ".join(candidate.strip().lower().split()) in normalized_options:
            return None

        return candidate

    def _scrape_selects(self, frame: Frame, frame_url: str) -> list[FormField]:
        fields = []
        try:
            elements = frame.query_selector_all("select")
        except PlaywrightError:
            return fields

        for el in elements:
            try:
                if not el.is_visible():
                    continue

                options = [
                    opt.strip() for opt in el.eval_on_selector_all(
                        "option", "opts => opts.map(o => o.textContent.trim())"
                    ) if opt.strip()
                ]
                label_text = self._label_for(frame, el)
                aria_label = el.get_attribute("aria-label")
                name_attr = el.get_attribute("name")
                id_attr = el.get_attribute("id")
                context_source = label_text or aria_label or name_attr or id_attr or "field"

                fields.append(FormField(
                    field_type="select",
                    name=name_attr,
                    element_id=id_attr,
                    label=label_text,
                    aria_label=aria_label,
                    required=el.get_attribute("required") is not None,
                    options=options,
                    frame_url=frame_url,
                    selector_hint=self._build_selector_hint(el),
                    context_key=_slugify(context_source),
                ))
            except PlaywrightError:
                continue

        return fields

    def _scrape_custom_dropdowns(self, frame: Frame, frame_url: str) -> list[FormField]:
        """
        React/Angular apps (Workday is notorious for this) often build dropdowns
        out of <div>/<button> combos rather than native <select>. We target
        common accessibility and class-name conventions:
            - role="combobox" / role="listbox" / role="button" acting as a select
            - [data-automation-id] patterns used heavily by Workday
            - class names containing "dropdown", "select" as a fallback heuristic
        """
        fields = []
        selector = (
            '[role="combobox"], [role="listbox"], '
            '[data-automation-id*="dropdown" i], [data-automation-id*="select" i], '
            'div[class*="dropdown" i], div[class*="select" i]:not(select)'
        )
        try:
            elements = frame.query_selector_all(selector)
        except PlaywrightError:
            return fields

        seen_selectors = set()  # crude de-dup since our selector list overlaps

        for el in elements:
            try:
                if not el.is_visible():
                    continue

                hint = self._build_selector_hint(el)
                if hint in seen_selectors:
                    continue
                seen_selectors.add(hint)

                # Custom dropdowns rarely expose <option> tags up front; the
                # options often only render after a click. We record what we
                # can now (label/current text) and flag it for the injection
                # phase to click-and-inspect if needed.
                current_text = el.inner_text().strip()[:120] if el.inner_text() else None
                label_text = self._label_for(frame, el) or current_text
                aria_label = el.get_attribute("aria-label")

                # Distinguish "this is a calendar/date widget" from a plain
                # custom dropdown -- they need different click-interaction
                # logic downstream (navigate a calendar vs. pick a list item).
                date_hints = " ".join(filter(None, [label_text, aria_label])).lower()
                widget_type = "custom_date_picker" if ("date" in date_hints or "dob" in date_hints) else "custom_dropdown"
                context_source = label_text or aria_label or "field"

                fields.append(FormField(
                    field_type=widget_type,
                    name=el.get_attribute("data-automation-id") or el.get_attribute("name"),
                    element_id=el.get_attribute("id"),
                    label=label_text,
                    aria_label=aria_label,
                    required=el.get_attribute("aria-required") == "true",
                    options=[],  # populated lazily during injection (Phase 3/4), not here
                    frame_url=frame_url,
                    selector_hint=hint,
                    context_key=_slugify(context_source),
                ))
            except PlaywrightError:
                continue

        return fields

    def _build_selector_hint(self, element_handle) -> Optional[str]:
        """
        Build a best-effort, reasonably stable CSS selector so the injection
        phase can re-locate this exact element without re-scraping.
        Prefers id > name > data-automation-id > a generated nth-of-type path.
        """
        try:
            el_id = element_handle.get_attribute("id")
            if el_id:
                return f'#{el_id}'

            name = element_handle.get_attribute("name")
            if name:
                tag = element_handle.evaluate("el => el.tagName.toLowerCase()")
                return f'{tag}[name="{name}"]'

            automation_id = element_handle.get_attribute("data-automation-id")
            if automation_id:
                return f'[data-automation-id="{automation_id}"]'

            # Fallback: ask the browser to compute a unique-ish path.
            return element_handle.evaluate("""
                el => {
                    function cssPath(el) {
                        if (!(el instanceof Element)) return '';
                        const path = [];
                        while (el.nodeType === Node.ELEMENT_NODE) {
                            let selector = el.nodeName.toLowerCase();
                            if (el.id) {
                                selector += '#' + el.id;
                                path.unshift(selector);
                                break;
                            } else {
                                let sib = el, nth = 1;
                                while (sib.previousElementSibling) {
                                    sib = sib.previousElementSibling;
                                    if (sib.nodeName.toLowerCase() === selector) nth++;
                                }
                                selector += `:nth-of-type(${nth})`;
                            }
                            path.unshift(selector);
                            el = el.parentNode;
                        }
                        return path.join(' > ');
                    }
                    return cssPath(el);
                }
            """)
        except PlaywrightError:
            return None

    def _bundle(self, url: str, fields: list[FormField]) -> dict:
        """Package results into a clean structure for the LLM matching layer.
        Ensures every context_key is unique -- two fields both labeled
        "Phone" (e.g. work + mobile) would otherwise collide as the same
        LLM-facing identifier."""
        seen_keys: dict[str, int] = {}
        for f in fields:
            base = f.context_key or "field"
            if base in seen_keys:
                seen_keys[base] += 1
                f.context_key = f"{base}_{seen_keys[base]}"
            else:
                seen_keys[base] = 1

        return {
            "source_url": url,
            "field_count": len(fields),
            "fields": [asdict(f) for f in fields],
        }


# --------------------------------------------------------------------------
# NOTE: The standalone CLI entrypoint that used to live here
# (`python jobScraper.py <url> --headed --out ...`) has been removed. It
# depended on scrape(url), which owned its own browser — that responsibility
# now belongs entirely to main.py. Use main.py to run the pipeline; this
# module is library code only.