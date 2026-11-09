"""tmuxsup's crash-window bookkeeping — pure logic, no tmux needed."""
import sys
import unittest
from collections import deque

sys.path.insert(0, ".")

from tmuxsup import CRASH_MAX, CRASH_WINDOW, crash_decision


class CrashWindowTest(unittest.TestCase):
    def test_restarts_below_threshold(self):
        crashes = deque()
        for i in range(CRASH_MAX - 1):
            self.assertEqual(
                crash_decision(crashes, float(i), window=300, max_crashes=5),
                "restart")

    def test_suspends_at_threshold_inside_window(self):
        crashes = deque()
        for i in range(CRASH_MAX):
            verdict = crash_decision(crashes, float(i) * 60,
                                     window=300, max_crashes=5)
        self.assertEqual(verdict, "suspend")

    def test_old_crashes_prune_out(self):
        # 4 crashes, then silence past the window, then 4 more: no suspend —
        # only 5 *within* the window trip the guard.
        crashes = deque()
        for i in range(4):
            crash_decision(crashes, float(i), window=300, max_crashes=5)
        verdict = "restart"
        for i in range(4):
            verdict = crash_decision(crashes, 400.0 + i, window=300,
                                     max_crashes=5)
        self.assertEqual(verdict, "restart")
        self.assertEqual(len(crashes), 4)

    def test_defaults_come_from_module_env(self):
        self.assertEqual(CRASH_MAX, 5)
        self.assertEqual(CRASH_WINDOW, 300.0)


if __name__ == "__main__":
    unittest.main()
