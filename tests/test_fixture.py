"""Static fixture contract; browser behavior belongs to a later stage."""

import unittest
from html.parser import HTMLParser
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "multi_step_application.html"


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")
        parser = Elements()
        parser.feed(cls.html)
        cls.tags = parser.tags

    def test_four_ordered_steps_with_only_first_visible_initially(self):
        steps = [attrs for tag, attrs in self.tags if tag == "section" and "data-step" in attrs]
        self.assertEqual([s["data-step"] for s in steps], ["1", "2", "3", "4"])
        self.assertNotIn("hidden", steps[0])
        self.assertTrue(all("hidden" in step for step in steps[1:]))

    def test_conditional_required_field_and_visible_validation_surface(self):
        ids = {attrs.get("id"): attrs for _, attrs in self.tags if attrs.get("id")}
        self.assertIn("hidden", ids["employer-followup"])
        self.assertEqual(ids["current-employer"]["name"], "current_employer")
        self.assertEqual(ids["validation-error"]["role"], "alert")
        self.assertIn("employer.required = show", self.html)
        self.assertIn("if (button.dataset.action === 'continue' && validateCurrentStep())", self.html)

    def test_repeated_email_and_distinct_navigation_submit_controls(self):
        names = [attrs.get("name") for tag, attrs in self.tags if tag == "input"]
        self.assertIn("email", names)
        self.assertIn("confirm_email", names)
        buttons = [attrs for tag, attrs in self.tags if tag == "button"]
        self.assertEqual(sum(button.get("type") == "submit" for button in buttons), 1)
        self.assertTrue(all(button.get("type") == "button" for button in buttons
                            if button.get("data-action") == "continue"))
        self.assertEqual(next(button for button in buttons if button.get("id") == "final-submit")["type"],
                         "submit")

    def test_fixture_has_no_external_runtime_assets(self):
        self.assertFalse(any(tag == "script" and "src" in attrs for tag, attrs in self.tags))
        self.assertFalse(any(tag == "link" and attrs.get("rel") == "stylesheet"
                             for tag, attrs in self.tags))
        self.assertIn("event.preventDefault()", self.html)


if __name__ == "__main__":
    unittest.main()
