"""padmode's systemd user service, as far as the settings window cares.

Whether it starts at login, whether it is running, and starting or restarting
it. Through ``systemctl --user``, the same commands ``install.sh`` uses -- so
the window never knows anything about unit files the installer does not.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

UNIT = "padmode.service"

#: ``systemctl is-enabled`` answers that mean "starts at login".
_ENABLED = {"enabled", "enabled-runtime", "linked", "linked-runtime", "alias"}


class Service:
    def __init__(self, run: Callable = subprocess.run, unit: str = UNIT) -> None:
        self._run = run
        self.unit = unit

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return self._run(["systemctl", "--user", *args, self.unit],
                         capture_output=True, text=True, timeout=10)

    def installed(self) -> bool:
        """False when there is no unit -- padmode run by hand, or the
        installer's service step declined."""
        return self.autostart() is not None

    def autostart(self) -> bool | None:
        """Starts at login? None if there is no service to ask about."""
        try:
            result = self._systemctl("is-enabled")
        except (OSError, subprocess.TimeoutExpired):
            return None
        state = result.stdout.strip()
        if state in ("", "not-found") or result.returncode == 4:
            return None
        return state in _ENABLED

    def set_autostart(self, on: bool) -> str | None:
        """Enable or disable. None on success, else why not.

        Only the login part: enabling does not start it now, disabling does
        not stop it -- turning off "start at login" should not take the pad
        away mid-session.
        """
        try:
            result = self._systemctl("enable" if on else "disable")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        if result.returncode != 0:
            return (result.stderr or result.stdout).strip() or "systemctl failed"
        return None

    def running(self) -> bool:
        try:
            return self._systemctl("is-active").stdout.strip() == "active"
        except (OSError, subprocess.TimeoutExpired):
            return False

    def start(self, restart: bool = False) -> str | None:
        try:
            result = self._systemctl("restart" if restart else "start")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        if result.returncode != 0:
            return (result.stderr or result.stdout).strip() or "systemctl failed"
        return None
