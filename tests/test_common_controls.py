"""ATS-neutral accessibility shapes from common application form patterns."""

import unittest

from jobagent.domain import ControlType, NavigationKind
from jobagent.snapshot import SnapshotNormalizer


def page(body: str) -> str:
    return ("### Page\n- Page URL: https://example.test/apply\n"
            "### Snapshot\n```yaml\n- main [ref=e1]:\n"
            '  - heading "Application" [level=2] [ref=e2]\n' + body + "```\n")


class CommonControlShapeTests(unittest.TestCase):
    def setUp(self):
        self.parser = SnapshotNormalizer()

    def test_grouped_custom_choice_and_radio_options(self):
        observation = self.parser.normalize(page(
            '  - group "Previously worked here?" [required] [ref=e3]:\n'
            '    - radio "Yes" [ref=e4]\n'
            '    - radio "No" [checked] [ref=e5]\n'
            '  - generic: Referral Source *\n'
            '  - button "Select One" [haspopup=listbox] [ref=e6]\n'
            '  - button "Next" [ref=e7]\n'), "grouped").observation
        self.assertEqual(len(observation.questions), 2)
        self.assertEqual(observation.questions[0].options, ("Yes", "No"))
        self.assertEqual(observation.questions[0].current_value, "No")
        self.assertTrue(observation.questions[1].required)
        self.assertEqual(observation.questions[1].label, "Referral Source")
        self.assertEqual(observation.navigation_controls[0].kind, NavigationKind.ADVANCE)

    def test_conventional_labeled_text_select_and_upload(self):
        observation = self.parser.normalize(page(
            '  - textbox "First Name" [required] [ref=e3]\n'
            '  - textbox "Email" [required] [ref=e4]\n'
            '  - combobox "State" [required] [ref=e5]:\n'
            '    - option "California" [ref=e6]\n'
            '    - option "Colorado" [ref=e7]\n'
            '  - button "Upload Resume" [ref=e8]\n'), "conventional").observation
        self.assertEqual([q.control_type for q in observation.questions],
                         [ControlType.TEXT, ControlType.TEXT, ControlType.CHOICE, ControlType.FILE])
        self.assertEqual(observation.questions[2].options, ("California", "Colorado"))

    def test_mixed_text_select_and_optional_question(self):
        observation = self.parser.normalize(page(
            '  - textbox "Last Name" [required] [ref=e3]\n'
            '  - combobox "How did you hear about us?" [optional] [ref=e4]: Select One\n'
            '  - button "Continue" [ref=e5]\n'), "mixed").observation
        self.assertEqual(len(observation.questions), 2)
        self.assertIs(observation.questions[1].required, False)
        self.assertEqual(observation.questions[1].current_value, "Select One")

    def test_modern_aria_typeahead_and_month(self):
        observation = self.parser.normalize(page(
            '  - textbox "School" [aria-autocomplete=list] [ref=e3]: Pacific\n'
            '  - listbox "Suggestions" [ref=e4]:\n'
            '    - option "Pacific College" [ref=e5]\n'
            '  - textbox "Available Month" [type=month] [ref=e6]\n'
            '  - button "Continue to Review" [ref=e7]\n'), "aria").observation
        self.assertEqual([q.control_type for q in observation.questions],
                         [ControlType.TYPEAHEAD, ControlType.DATE])
        self.assertEqual(observation.questions[0].options, ("Pacific College",))
        self.assertFalse(observation.questions[0].selection_confirmed)
        self.assertEqual(observation.questions[1].date_format, "month")
        self.assertEqual(observation.navigation_controls[0].kind, NavigationKind.ADVANCE)


if __name__ == "__main__":
    unittest.main()
