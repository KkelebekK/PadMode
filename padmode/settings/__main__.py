"""``python -m padmode.settings`` -- the settings window process."""

import sys

from . import unavailable_reason

reason = unavailable_reason()
if reason:
    print(f"padmode settings: {reason}", file=sys.stderr)
    sys.exit(1)

from .app import main  # noqa: E402

sys.exit(main())
