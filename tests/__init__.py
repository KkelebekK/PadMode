"""Test suite.

Run with:  python -m unittest discover -s tests -t .

Stdlib unittest rather than pytest, so contributors need nothing installed
beyond what the app itself requires.
"""

import logging
import os
import unittest

#: Set ``PADMODE_INTEGRATION=1`` to run the tests that drive the real desktop.
#:
#: Almost everything here is safe to run on the machine you are sitting at:
#: creating a uinput *gamepad* only gives padmode something to read. The
#: exception is the handful of tests that also build padmode's virtual mouse
#: and keyboard, because what those emit is not simulated -- the pointer
#: moves, the screen scrolls, and windows alt-tab on whatever session is
#: running. Developing padmode on your own desktop means those fire every time
#: the suite runs, which is disruptive enough to have been asked about.
#:
#: Off by default, and that has a real cost: the uinput *output* path loses its
#: only automated proof. What survives in the default run is
#: ``test_integration.py``, which creates a pad but no virtual output, so
#: discovery, grabbing and the disconnect path are still covered -- which is
#: most of what an accidental change is likely to break.
#:
#: The rest is bought back before a release, by running:
#:
#:     PADMODE_INTEGRATION=1 python -m unittest discover -s tests -t .
#:
#: on a session you do not mind being typed into.
DRIVES_THE_DESKTOP = os.environ.get("PADMODE_INTEGRATION") == "1"

#: Decorator for those tests. Class-level, since a fixture that builds the
#: virtual devices makes every test in the class one of them.
drives_the_desktop = unittest.skipUnless(
    DRIVES_THE_DESKTOP,
    "moves the real pointer, scrolls and alt-tabs the running session; "
    "set PADMODE_INTEGRATION=1 to run it",
)

# padmode logs warnings for missing external tools, which several tests trigger
# deliberately. Attaching a handler here stops logging's "last resort" fallback
# from printing them to stderr and cluttering the run. assertLogs still works:
# it installs its own handler for the duration of the block.
logging.getLogger("padmode").addHandler(logging.NullHandler())
