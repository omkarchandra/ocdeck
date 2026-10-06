import unittest

from ocdeck.agents_layout import (
    CHROME, STATE_CODES, TERM_CODES, column_widths, legend, state_cell, term_cell, term_kind,
)


class CellTests(unittest.TestCase):
    def test_state_cells_are_short_and_keep_the_age(self):
        self.assertEqual(state_cell("busy", "2m"), "● RUN 2m")
        self.assertEqual(state_cell("permission", "1h"), "! PERM 1h")
        self.assertEqual(state_cell("review"), "◑ REV")
        self.assertEqual(state_cell("closed", "3d"), "○ CLSD 3d")
        for state in STATE_CODES:
            with self.subTest(state=state):
                self.assertLessEqual(len(state_cell(state, "59m")), 9)
        self.assertEqual(state_cell("weird"), "○ WEIR")

    def test_term_cells_cover_every_terminal_kind(self):
        self.assertEqual(term_cell(term_kind(attached=True, tmux=True, live=True)), "●OPN")
        self.assertEqual(term_cell(term_kind(attached=False, tmux=True, live=True)), "○TMX")
        self.assertEqual(term_cell(term_kind(attached=False, tmux=False, live=True)), "◆DIR")
        self.assertEqual(term_cell(term_kind(attached=False, tmux=False, live=False)), "○SRV")
        self.assertEqual(term_cell(term_kind(attached=False, tmux=False, live=False, closed=True)), "○SAV")
        self.assertTrue(all(len(code) == 4 for code in TERM_CODES.values()))

    def test_legend_spells_codes_out_for_the_focus_strip(self):
        self.assertEqual(legend("busy", "○TMX"), "running · background tmux")
        self.assertEqual(legend("review", "◆DIR"), "waiting for you · direct terminal tab")


class WidthTests(unittest.TestCase):
    def test_every_column_through_runtime_fits_from_59_columns(self):
        for width in range(59, 260):
            with self.subTest(width=width):
                widths = column_widths(width)
                self.assertLessEqual(widths.used() + CHROME, width)
                for name in ("state", "term", "session", "project", "age", "runtime"):
                    self.assertGreater(getattr(widths, name), 0, name)

    def test_tiny_windows_still_fit_state_session_age_and_runtime(self):
        for width in range(40, 59):
            with self.subTest(width=width):
                widths = column_widths(width)
                self.assertLessEqual(widths.used() + CHROME, width)
                self.assertTrue(widths.state and widths.session and widths.age and widths.runtime)

    def test_detail_is_the_only_column_that_hides(self):
        narrow, wide = column_widths(60), column_widths(140)
        self.assertEqual(narrow.detail, 0)
        self.assertGreaterEqual(wide.detail, 10)

    def test_runtime_uses_full_names_only_when_wide(self):
        self.assertFalse(column_widths(159).full_runtime)
        self.assertTrue(column_widths(160).full_runtime)

    def test_names_grow_before_detail(self):
        self.assertEqual(column_widths(200).session, 34)
        self.assertEqual(column_widths(200).project, 16)


if __name__ == "__main__":
    unittest.main()
