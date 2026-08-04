"""
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
    selector_hint: Optional[str] = None  # real CSS/attribute selector Playwright uses to locate this element
    context_key: Optional[str] = None    # human-readable identifier sent to the LLM -- never used as a CSS selector
    option_selectors: dict = field(default_factory=dict)  # radio_group only: option label -> that radio's real selector

    def best_context(self) -> str:
        """Return the most useful human-readable description of what this field asks."""
        return self.label or self.placeholder or self.aria_label or self.name or self.element_id or "UNKNOWN_FIELD"


# Phrases that give the LLM essentially zero signal about what's being
# asked (e.g. a date picker's generic placeholder rather than the actual
# question). When the best label found matches one of these, the search
# widens instead of accepting it.
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


# Date labels that are correct and complete for a one-off date field, but
# ambiguous when repeated identically across multiple form sections (e.g.
# an education entry vs. each work_history entry). Not flagged as vague
# in isolation, but need a section prefix to disambiguate.
_GENERIC_DATE_LABELS = ("start date", "end date", "from", "to", "from date", "to date")


def _is_generic_date_label(text: Optional[str]) -> bool:
    if not text:
        return True
    normalized = " ".join(text.strip().lower().split())
    return normalized in _GENERIC_DATE_LABELS


def _slugify(text: str, max_len: int = 80) -> str:
    """
    Turns label text into a stable, LLM-readable identifier (e.g.
    "graduation_date") rather than an opaque framework-generated ID.
    Truncates at a word boundary, never mid-word, so disambiguating words
    at the end of a long label aren't silently dropped. This field_id is
    never used as a CSS selector (see selector_hint for that), so there's
    no strict length requirement forcing a hard cut.
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
        Scrape an already-navigated Playwright page (and its frames).
        Call this after page.goto() has already run — main.py owns the
        browser lifecycle.
        """
        # Give dynamically-rendered (React/Angular) forms a moment to mount.
        page.wait_for_timeout(1500)

        all_fields: list[FormField] = []

        all_fields.extend(self._scrape_frame(page.main_frame))

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                all_fields.extend(self._scrape_frame(frame))
            except PlaywrightError as e:
                # Some frames (ads, trackers, analytics) are inaccessible
                # or irrelevant -- log and move on.
                print(f"[warn] Could not scrape frame {frame.url}: {e}", file=sys.stderr)

        return self._bundle(page.url, all_fields)

    # ---- internals -----------------------------------------------------

    def _scrape_frame(self, frame: Frame) -> list[FormField]:
        """Scrape standard inputs/selects/textareas plus custom dropdowns from one frame."""
        fields: list[FormField] = []
        frame_url = frame.url or "about:blank"

        fields.extend(self._scrape_standard_inputs(frame, frame_url))
        fields.extend(self._scrape_radio_groups(frame, frame_url))
        fields.extend(self._scrape_aria_radio_groups(frame, frame_url))
        fields.extend(self._scrape_generic_button_groups(frame, frame_url))
        fields.extend(self._scrape_selects(frame, frame_url))
        fields.extend(self._scrape_custom_dropdowns(frame, frame_url))

        return fields

    def _immediate_label(self, frame: Frame, element_handle) -> Optional[str]:
        """
        Resolves <label for="id">, parent <label>, or aria-labelledby with
        NO vague-text filtering — returns whatever it finds as-is, even if
        short (e.g. "No", "Yes"). Used specifically for an individual
        radio's own option text, where a short valid answer must not be
        treated as "too vague" and replaced. Use _label_for() for field
        labels/questions instead.
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
                parent_label = element_handle.evaluate_handle("el => el.closest('label')")
                if parent_label:
                    el = parent_label.as_element()
                    text = el.inner_text().strip() if el else None
                    if text:
                        candidate = text

            if not candidate:
                labelledby = element_handle.get_attribute("aria-labelledby")
                if labelledby:
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

    def _label_for(self, frame: Frame, element_handle, placeholder: Optional[str] = None) -> Optional[str]:
        """
        Resolves the best human-readable label/question for a field,
        widening the search if what's found is missing or too vague to
        be useful. Strategies, in order:

          1. <label for="id">
          2. Parent <label> wrapping the input
          3. aria-labelledby
          4. This element's own `placeholder` (per-input, so it actually
             distinguishes this field from siblings)
          5. Nearest ancestor <fieldset>'s <legend>, or nearest ancestor's
             own non-input text — for custom form libraries that render a
             question visually without a semantic <label> element

        Placeholder is tried before the broader ancestor-context fallback:
        a shared <fieldset><legend>Address</legend> wrapping 4 separate
        street/city/state/zip inputs would otherwise return the identical
        "Address" text for all 4, whereas each input's own placeholder
        actually distinguishes them.

        NOTE: use _immediate_label() instead for an individual radio's own
        option text — this function's vague-check-and-broaden behavior
        would wrongly replace a short valid answer like "No".
        """
        candidate = self._immediate_label(frame, element_handle)

        if candidate and not _is_vague_label(candidate):
            return candidate

        if placeholder and not _is_vague_label(placeholder):
            return placeholder

        broader = self._find_broader_context(element_handle)
        if broader:
            return broader

        return candidate or placeholder

    def _find_broader_context(self, element_handle) -> Optional[str]:
        """
        Fallback for when the immediate label/placeholder is missing or
        too vague. Walks up the DOM looking for the nearest ancestor
        <fieldset>'s <legend>, then for the nearest ancestor with a
        non-input child (or its own direct text) that reads like a real
        question — common in custom form libraries (Greenhouse, Ashby,
        Workday) that render a question visually without a semantic
        <label>.
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
        Walks further up the DOM than _find_broader_context, looking for
        the nearest preceding section heading (h1-h6, or a <legend> acting
        as a section divider — e.g. "Education", "Work Experience"). Used
        to disambiguate generic per-entry fields like "Start Date"/"End
        Date" that repeat identically across multiple form sections.
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

                # Skip noise: hidden fields, submit/button inputs,
                # honeypots. "radio" is skipped here deliberately -- radio
                # inputs are grouped into one logical field by
                # _scrape_radio_groups() instead.
                if input_type in ("hidden", "submit", "button", "image", "radio"):
                    continue
                # File inputs are exempt from the visibility check: custom
                # upload widgets almost always hide the real <input
                # type="file"> via CSS while keeping it functional, and
                # set_input_files() works fine on a non-visible file input.
                if input_type != "file" and not el.is_visible():
                    continue

                tag = el.evaluate("el => el.tagName.toLowerCase()")
                placeholder = el.get_attribute("placeholder")
                aria_label = el.get_attribute("aria-label")
                name_attr = el.get_attribute("name")
                id_attr = el.get_attribute("id")
                label_text = self._label_for(frame, el, placeholder)

                field_type = "textarea" if tag == "textarea" else input_type

                # Some ATS platforms build date pickers on a plain
                # type="text" input rather than native type="date".
                # Reclassify based on surrounding hints so injection.py
                # treats it as a date.
                if field_type == "text":
                    date_hints = " ".join(filter(None, [
                        aria_label, placeholder, name_attr, id_attr, label_text,
                    ])).lower()
                    if "date" in date_hints or "dob" in date_hints:
                        field_type = "date"

                # A readonly/aria-readonly text input can never accept
                # fill() -- its only real interaction is click-to-open, so
                # it's reclassified as a custom_dropdown regardless of
                # which ARIA role or class name pattern it uses.
                if field_type == "text" and (
                    el.get_attribute("readonly") is not None
                    or el.get_attribute("aria-readonly") == "true"
                ):
                    field_type = "custom_dropdown"

                # Repeating form sections (e.g. an education entry's
                # start/end dates vs. each work_history entry's) often
                # share the same generic label. Prefix with the nearest
                # section heading when the label is this generic, so the
                # LLM sees "Education — Start Date" instead of an
                # ambiguous "Start Date" repeated across every section.
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
                continue  # element detached / stale — skip rather than crash the run

        return fields

    def _scrape_radio_groups(self, frame: Frame, frame_url: str) -> list[FormField]:
        """
        Groups <input type="radio"> elements sharing the same `name` into
        one logical FormField (like a select), rather than emitting each
        radio as an isolated field the LLM has no way to reason about
        collectively. Radios without a `name` can't be reliably grouped
        and are skipped.
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

                    # Each radio's own label is the OPTION text ("Yes,
                    # OPT" / "No") -- use the unfiltered _immediate_label,
                    # not _label_for's vague-check-and-broaden behavior.
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

            # Resolve the group's shared question from all radios
            # together, not per-radio (searching from a single radio just
            # re-finds that radio's own paired option label).
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

    def _scrape_aria_radio_groups(self, frame: Frame, frame_url: str) -> list[FormField]:
        """
        Groups ARIA-pattern custom radio buttons -- role="radio" inside
        role="radiogroup" -- the standards-based way to build a
        custom-styled radio UI without native <input type="radio">.
        Accessibility-conscious ATS platforms (Ashby included) commonly
        use this for Yes/No toggles, which _scrape_radio_groups (native
        inputs only) can't see.

        Reuses the same "radio_group" field_type and option_selectors
        shape as _scrape_radio_groups, so injection.py's
        _inject_radio_group() handles these with zero further changes --
        only activation differs (click vs. check), already handled there.
        """
        fields: list[FormField] = []
        try:
            radiogroups = frame.query_selector_all('[role="radiogroup"]')
        except PlaywrightError:
            return fields

        for group_idx, group_el in enumerate(radiogroups):
            try:
                if not group_el.is_visible():
                    continue

                option_elements = group_el.query_selector_all('[role="radio"]')
                if not option_elements:
                    continue

                options: list[str] = []
                option_selectors: dict[str, str] = {}
                required = group_el.get_attribute("aria-required") == "true"

                for opt_el in option_elements:
                    try:
                        if not opt_el.is_visible():
                            continue
                        option_text = (
                            self._immediate_label(frame, opt_el)
                            or (opt_el.get_attribute("aria-label") or "").strip()
                            or (opt_el.inner_text() or "").strip()
                        )
                        if not option_text:
                            continue
                        option_hint = self._build_selector_hint(opt_el)
                        if option_text not in option_selectors:
                            options.append(option_text)
                            option_selectors[option_text] = option_hint
                    except PlaywrightError:
                        continue

                if not options:
                    continue

                # The group's own aria-label/aria-labelledby is the most
                # reliable question source when present.
                group_label = (group_el.get_attribute("aria-label") or "").strip() or None
                if not group_label:
                    labelledby = group_el.get_attribute("aria-labelledby")
                    if labelledby:
                        texts = []
                        for ref_id in labelledby.split():
                            ref_el = frame.query_selector(f"#{ref_id}")
                            if ref_el:
                                t = ref_el.inner_text().strip()
                                if t:
                                    texts.append(t)
                        if texts:
                            group_label = " ".join(texts)
                if not group_label:
                    broader = self._find_broader_context(group_el)
                    if broader and not _is_vague_label(broader):
                        group_label = broader

                context_source = group_label or f"aria_radiogroup_{group_idx}"
                fields.append(FormField(
                    field_type="radio_group",
                    name=f"aria_radiogroup_{group_idx}",
                    label=group_label,
                    required=required,
                    options=options,
                    frame_url=frame_url,
                    selector_hint=None,
                    context_key=_slugify(context_source),
                    option_selectors=option_selectors,
                ))
            except PlaywrightError:
                continue

        return fields

    def _scrape_generic_button_groups(self, frame: Frame, frame_url: str) -> list[FormField]:
        """
        Catches button-styled "choice" questions with no special semantic
        markup at all -- no native radio, no ARIA radio, just plain
        sibling <button> elements (e.g. a Yes/No pill-button pair) whose
        only signal of being a mutually-exclusive choice is structural:
        short text, sitting together as siblings, near a preceding
        question. Markup-agnostic by design, so it doesn't need a new
        pattern-specific scraper for every site.

        Defense layers against false positives (marketing/nav buttons
        instead of real form questions):
          1. SCOPE: if the page has any <form> elements, search only
             inside them.
          2. VOCABULARY: exclude an action/CTA word list (submit,
             contact, sales, pricing, learn more, login, etc.) — still
             applies on SPAs with no real <form> tag.
          3. REQUIRED REAL LABEL: a button cluster with no genuine,
             non-vague preceding question text is dropped entirely, not
             kept unlabeled.

        Candidate heuristics (all required): <button> or [role="button"],
        visible, enabled; short visible text (<=40 chars); not already
        inside a role="radiogroup"; at least 2 such buttons share the
        same immediate parent.

        Remaining trade-off: this is still a heuristic (e.g. adjacent "+"
        / "-" stepper buttons could misfire if preceded by
        question-looking text). The review gate remains the actual safety
        net for the residual risk -- a wrongly-scraped field is an extra
        item to glance at, never a silent wrong submission.
        """
        try:
            raw_groups = frame.evaluate(r"""
                () => {
                    function cssPath(el) {
                        const path = [];
                        while (el && el.nodeType === Node.ELEMENT_NODE) {
                            let selector = el.nodeName.toLowerCase();
                            if (el.id) {
                                selector += '#' + el.id;
                                path.unshift(selector);
                                break;
                            }
                            let sib = el, nth = 1;
                            while (sib.previousElementSibling) {
                                sib = sib.previousElementSibling;
                                if (sib.nodeName === el.nodeName) nth++;
                            }
                            selector += `:nth-of-type(${nth})`;
                            path.unshift(selector);
                            el = el.parentElement;
                        }
                        return path.join(' > ');
                    }

                    function cleanText(node) {
                        if (!node) return '';
                        return (node.innerText || node.textContent || '').trim().replace(/\s+/g, ' ');
                    }

                    const ACTION_WORDS = [
                        'submit', 'apply', 'next', 'continue', 'save', 'cancel', 'back',
                        'upload', 'browse', 'remove', 'delete', 'clear', 'reset', 'close',
                        'preview', 'download', 'print', 'edit', 'add', 'attach',
                        'choose file', 'select file', 'sign in', 'sign up', 'log in',
                        'register', 'contact', 'sales', 'team', 'demo', 'pricing',
                        'learn more', 'get started', 'book a', 'talk to', 'chat',
                        'subscribe', 'newsletter', 'follow', 'share', 'tweet',
                        'home', 'about us', 'careers', 'blog', 'login', 'menu',
                        'search', 'help', 'support', 'faq', 'terms', 'privacy',
                    ];
                    function looksLikeAction(text) {
                        const t = text.toLowerCase();
                        return ACTION_WORDS.some(w => t.includes(w));
                    }

                    const forms = Array.from(document.querySelectorAll('form'));
                    const searchRoots = forms.length > 0 ? forms : [document];

                    const seen = new Set();
                    const candidates = [];
                    for (const root of searchRoots) {
                        for (const el of root.querySelectorAll('button, [role="button"]')) {
                            if (seen.has(el)) continue;
                            seen.add(el);
                            if (el.closest('[role="radiogroup"]')) continue;
                            if (el.disabled) continue;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') continue;
                            const rect = el.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            const text = cleanText(el);
                            if (!text || text.length > 40) continue;
                            if (looksLikeAction(text)) continue;
                            candidates.push(el);
                        }
                    }

                    const groups = new Map();
                    for (const el of candidates) {
                        const parent = el.parentElement;
                        if (!parent) continue;
                        if (!groups.has(parent)) groups.set(parent, []);
                        groups.get(parent).push(el);
                    }

                    const result = [];
                    for (const [parent, elements] of groups.entries()) {
                        if (elements.length < 2) continue;

                        let label = null;
                        const fs = parent.closest('fieldset');
                        if (fs) {
                            const legend = fs.querySelector('legend');
                            const t = legend ? cleanText(legend) : '';
                            if (t && t.length > 2 && t.length < 200) label = t;
                        }
                        if (!label) {
                            let node = parent;
                            for (let depth = 0; depth < 5 && node; depth++) {
                                for (const child of node.children) {
                                    if (elements.includes(child)) continue;
                                    if (child.tagName === 'BUTTON') continue;
                                    const t = cleanText(child);
                                    if (t && t.length > 2 && t.length < 200) { label = t; break; }
                                }
                                if (label) break;
                                node = node.parentElement;
                            }
                        }

                        result.push({
                            label,
                            options: elements.map(el => ({ text: cleanText(el), selector: cssPath(el) })),
                        });
                    }
                    return result;
                }
            """)
        except PlaywrightError:
            return []

        fields: list[FormField] = []
        for idx, group in enumerate(raw_groups or []):
            options: list[str] = []
            option_selectors: dict[str, str] = {}
            for opt in group.get("options", []):
                text = (opt.get("text") or "").strip()
                selector = opt.get("selector")
                if not text or not selector:
                    continue
                if text not in option_selectors:
                    options.append(text)
                    option_selectors[text] = selector

            if len(options) < 2:
                continue

            # Strongest defense layer: require a real question label.
            # Groups with no usable label are dropped entirely, since an
            # unlabeled button cluster is exactly the shape a false
            # positive takes.
            group_label = group.get("label")
            if not group_label or _is_vague_label(group_label):
                continue

            context_source = group_label
            fields.append(FormField(
                field_type="radio_group",
                name=f"button_group_{idx}",
                label=group_label,
                required=False,
                options=options,
                frame_url=frame_url,
                selector_hint=None,
                context_key=_slugify(context_source),
                option_selectors=option_selectors,
            ))

        return fields

    def _find_group_question(self, radio_elements: list, option_texts: list[str]) -> Optional[str]:
        """
        Resolves the shared question for a group of radios, as distinct
        from any individual radio's own option label: finds the nearest
        DOM ancestor common to every radio in the group, then searches
        for a <fieldset><legend> or heading-like text from that shared
        point. Any candidate that exactly matches one of the group's own
        option texts is rejected outright — a real question should never
        be identical to one of its own answers.
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
                            // Skip form controls AND their paired labels
                            // -- per-option text, not the group question.
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
        React/Angular apps (Workday especially) often build dropdowns out
        of <div>/<button> combos rather than native <select>. Targets
        common accessibility/class-name conventions: role="combobox" /
        role="listbox", data-automation-id patterns, and class names
        containing "dropdown"/"select" as a fallback heuristic.
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

                # Custom dropdowns rarely expose <option> tags up front;
                # options often only render after a click. Record what we
                # can now and flag it for the injection phase to
                # click-and-inspect if needed.
                current_text = el.inner_text().strip()[:120] if el.inner_text() else None
                label_text = self._label_for(frame, el) or current_text
                aria_label = el.get_attribute("aria-label")

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
                    options=[],  # populated lazily during injection, not here
                    frame_url=frame_url,
                    selector_hint=hint,
                    context_key=_slugify(context_source),
                ))
            except PlaywrightError:
                continue

        return fields

    def _build_selector_hint(self, element_handle) -> Optional[str]:
        """
        Best-effort, reasonably stable CSS selector so the injection phase
        can re-locate this element without re-scraping. Prefers id > name
        > data-automation-id > a generated nth-of-type path.
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
        """Package results for the LLM matching layer, de-duplicating
        context_keys so two fields with the same label don't collide."""
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