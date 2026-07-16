"""
job_agent_scraper.py

Phase 2: Extraction Layer for the local job-application agent.

Responsibilities:
    - Launch a browser and navigate to a target job posting URL.
    - Recursively scan the main page AND all nested iframes (Workday, Taleo,
      Greenhouse, Lever, etc. commonly embed forms in cross-origin iframes).
    - Extract standard form fields (input, textarea, select) AND
      "custom" JS-framework dropdowns (div/button elements styled as
      selects, using role="combobox"/"listbox" or common class patterns).
    - Bundle everything into a clean, structured dict ready to be handed
      to the LLM matching layer in Phase 3.

No LLM/Ollama code lives here — this module's only job is DOM -> structured data.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

from playwright.sync_api import (
    sync_playwright,
    Page,
    Frame,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class FormField:
    """A single scraped form field, normalized for downstream LLM matching."""
    field_type: str            # "text", "textarea", "select", "checkbox", "radio", "custom_dropdown", etc.
    name: Optional[str] = None
    element_id: Optional[str] = None
    label: Optional[str] = None        # text from an associated <label>
    placeholder: Optional[str] = None
    aria_label: Optional[str] = None
    required: bool = False
    options: list = field(default_factory=list)  # for select / custom dropdowns
    frame_url: Optional[str] = None    # which frame (main page or iframe src) this came from
    selector_hint: Optional[str] = None  # best-effort CSS selector for later injection

    def best_context(self) -> str:
        """Return the most useful human-readable description of what this field asks."""
        return self.label or self.placeholder or self.aria_label or self.name or self.element_id or "UNKNOWN_FIELD"


# --------------------------------------------------------------------------
# Scraper
# --------------------------------------------------------------------------

class JobFormScraper:
    def __init__(self, headless: bool = False, nav_timeout_ms: int = 30000):
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms

    # ---- public entrypoint -------------------------------------------------

    def scrape(self, url: str) -> dict:
        """
        Navigate to `url` and return a bundled dict of all scraped fields,
        ready for JSON serialization / LLM prompting.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context()
            page = context.new_page()

            try:
                self._safe_goto(page, url)
            except PlaywrightTimeoutError:
                browser.close()
                raise RuntimeError(f"Timed out loading {url}. The page may be slow, "
                                    f"behind a login wall, or blocking automation.")

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

            browser.close()

            return self._bundle(url, all_fields)

    # ---- internals -----------------------------------------------------

    def _safe_goto(self, page: Page, url: str):
        page.goto(url, timeout=self.nav_timeout_ms, wait_until="domcontentloaded")
        try:
            # Best-effort wait for network to settle (SPAs love to keep polling,
            # so we don't hard-fail if it never fully idles).
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass  # Not fatal — proceed with whatever has rendered.

    def _scrape_frame(self, frame: Frame) -> list[FormField]:
        """Scrape standard inputs/selects/textareas plus custom dropdowns from one frame."""
        fields: list[FormField] = []
        frame_url = frame.url or "about:blank"

        fields.extend(self._scrape_standard_inputs(frame, frame_url))
        fields.extend(self._scrape_selects(frame, frame_url))
        fields.extend(self._scrape_custom_dropdowns(frame, frame_url))

        return fields

    def _label_for(self, frame: Frame, element_handle) -> Optional[str]:
        """
        Try several strategies to find the human-readable label for an element:
          1. <label for="id">
          2. Parent <label> wrapping the input
          3. aria-labelledby pointing to another element's text
        """
        try:
            el_id = element_handle.get_attribute("id")
            if el_id:
                label_el = frame.query_selector(f'label[for="{el_id}"]')
                if label_el:
                    text = label_el.inner_text().strip()
                    if text:
                        return text

            # Parent <label> wrapping the field (common in custom form libraries)
            parent_label = element_handle.evaluate_handle(
                "el => el.closest('label')"
            )
            if parent_label:
                text = parent_label.as_element().inner_text().strip() if parent_label.as_element() else None
                if text:
                    return text

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
                    return " ".join(texts)
        except PlaywrightError:
            pass
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
                if input_type in ("hidden", "submit", "button", "image", "file"):
                    continue
                if not el.is_visible():
                    continue

                tag = el.evaluate("el => el.tagName.toLowerCase()")

                fields.append(FormField(
                    field_type="textarea" if tag == "textarea" else input_type,
                    name=el.get_attribute("name"),
                    element_id=el.get_attribute("id"),
                    label=self._label_for(frame, el),
                    placeholder=el.get_attribute("placeholder"),
                    aria_label=el.get_attribute("aria-label"),
                    required=el.get_attribute("required") is not None
                              or el.get_attribute("aria-required") == "true",
                    frame_url=frame_url,
                    selector_hint=self._build_selector_hint(el),
                ))
            except PlaywrightError:
                continue  # Element detached / stale — skip rather than crash the run.

        return fields

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

                fields.append(FormField(
                    field_type="select",
                    name=el.get_attribute("name"),
                    element_id=el.get_attribute("id"),
                    label=self._label_for(frame, el),
                    aria_label=el.get_attribute("aria-label"),
                    required=el.get_attribute("required") is not None,
                    options=options,
                    frame_url=frame_url,
                    selector_hint=self._build_selector_hint(el),
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

                fields.append(FormField(
                    field_type="custom_dropdown",
                    name=el.get_attribute("data-automation-id") or el.get_attribute("name"),
                    element_id=el.get_attribute("id"),
                    label=self._label_for(frame, el) or current_text,
                    aria_label=el.get_attribute("aria-label"),
                    required=el.get_attribute("aria-required") == "true",
                    options=[],  # populated lazily during injection (Phase 3/4), not here
                    frame_url=frame_url,
                    selector_hint=hint,
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
        """Package results into a clean structure for the LLM matching layer."""
        return {
            "source_url": url,
            "field_count": len(fields),
            "fields": [asdict(f) for f in fields],
        }


# --------------------------------------------------------------------------
# CLI entrypoint
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape form fields from a job application page (Phase 2)."
    )
    parser.add_argument("url", help="URL of the job application form to scrape.")
    parser.add_argument(
        "--headed", action="store_true",
        help="Run with a visible browser window (useful for debugging login walls, CAPTCHAs, etc.)"
    )
    parser.add_argument(
        "--out", default="scraped_fields.json",
        help="Path to write the bundled JSON output (default: scraped_fields.json)"
    )
    args = parser.parse_args()

    scraper = JobFormScraper(headless=not args.headed)

    try:
        result = scraper.scrape(args.url)
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Scraped {result['field_count']} fields from {args.url}")
    print(f"Output written to {args.out}")

    # Quick human-readable preview in the terminal
    for field_data in result["fields"][:15]:
        print(f"  - [{field_data['field_type']}] {field_data['label'] or field_data['placeholder'] or field_data['name']}")
    if result["field_count"] > 15:
        print(f"  ... and {result['field_count'] - 15} more (see {args.out})")


if __name__ == "__main__":
    main()