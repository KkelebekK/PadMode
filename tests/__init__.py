"""Test suite.

Run with:  python -m unittest discover -s tests -t .

Stdlib unittest rather than pytest, so contributors need nothing installed
beyond what the app itself requires.
"""

import logging

# padmode logs warnings for missing external tools, which several tests trigger
# deliberately. Attaching a handler here stops logging's "last resort" fallback
# from printing them to stderr and cluttering the run. assertLogs still works:
# it installs its own handler for the duration of the block.
logging.getLogger("padmode").addHandler(logging.NullHandler())
