"""``python -m padmode.osk`` -- the keyboard renderer process.

Kept to one line of logic so that importing the package costs nothing: the
daemon imports :mod:`padmode.osk.protocol`, and everything GTK lives behind
:mod:`padmode.osk.renderer`, which is only reached from here.
"""

import sys

from .renderer import main

sys.exit(main())
