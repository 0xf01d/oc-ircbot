import os
import sys

# The project is flat (bot.py, brain.py, leak_guard.py at the repo root), so
# make the root importable for the unit tests regardless of where pytest runs.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
