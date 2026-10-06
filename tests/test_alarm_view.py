import unittest
from dataclasses import dataclass

from ocdeck.alarm_view import health_text
from ocdeck.sentinel.health import SentinelHealth


@dataclass
class FakeReport:
    health: SentinelHealth


class HealthTextTests(unittest.TestCase):
    def test_summary_line_reflects_the_real_alarm_count_not_a_hardcoded_hint(self):
        # Regression: the non-detail branch once said the literal "6 alarms"
        # (the key binding for the tab, not a count) regardless of how many
        # alarms actually exist.
        health = SentinelHealth("OBSERVED", (), alarm_count=11, critical_count=2)
        text = health_text(FakeReport(health), details=False).plain
        self.assertIn("ALARMS(11)", text)
        self.assertIn("2 CRITICAL", text)
        self.assertNotRegex(text, r"\b\d+ alarms\b")

    def test_zero_alarms_renders_cleanly(self):
        health = SentinelHealth("OBSERVED", (), alarm_count=0)
        text = health_text(FakeReport(health), details=False).plain
        self.assertIn("ALARMS(0)", text)
        self.assertNotIn("CRITICAL", text)


if __name__ == "__main__":
    unittest.main()
