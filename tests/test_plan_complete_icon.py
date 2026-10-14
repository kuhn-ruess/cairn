"""The complete chip on a Plan card says whether the task is open or done.

Run with ``python -m unittest discover tests``.

The report: on Today's Plan both states looked ticked. An open task showed a
bare check mark (``bi-check2``), a done one the filled check circle, so the only
difference was the size. Open is the empty circle, as on every list row
(``_list_body.html``) and on the rail pills; done stays the filled green check
circle. The card is rendered by the template and rebuilt by plan-view.js (drop
from a rail, quick add, the toggle after a click), so both are checked.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "application/templates/tasks/_plan_timeline.html"
SCRIPT = ROOT / "application/static/js/plan-view.js"

DONE = "bi-check-circle-fill text-success"
OPEN = "bi-circle"


class PlanCompleteIconTests(unittest.TestCase):

    def test_template_open_card_shows_empty_circle(self):
        src = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(f"'{DONE}' if it.done else '{OPEN}'", src)

    def test_script_never_draws_a_check_for_an_open_card(self):
        src = SCRIPT.read_text(encoding="utf-8")
        chips = re.findall(r'plan-complete" title="Mark done"><i class="bi ([^"]+)">', src)
        self.assertTrue(chips, "plan-view.js no longer builds a complete chip")
        for icon in chips:
            self.assertEqual(icon, OPEN)
        self.assertIn(f'wasDone ? "bi {OPEN}" : "bi {DONE}"', src)
        self.assertNotRegex(src, r'"bi bi-check2"')


if __name__ == "__main__":
    unittest.main()
