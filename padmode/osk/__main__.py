"""``python -m padmode.osk`` -- the keyboard renderer process.

Kept to one line of logic so that importing the package costs nothing: the
daemon imports :mod:`padmode.osk.protocol` and :mod:`padmode.osk.client`, and
everything GTK lives behind :mod:`padmode.osk.app`, which is only reached from
here.
"""

import sys

from .app import main

sys.exit(main())
