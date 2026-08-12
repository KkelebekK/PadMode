"""Executes bindings, and degrades gracefully when tools are missing.

Policy (CLAUDE.md §4): a missing external command disables that action and logs
*once*. It never crashes the daemon and never spams the journal -- somebody
without ``playerctl`` should still get working volume control.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable

from .builtin import ACTIONS, ActionContext

log = logging.getLogger(__name__)

#: Injected in tests so nothing is actually spawned.
Spawner = Callable[[list[str]], None]


def _spawn(argv: list[str]) -> None:
    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class ActionRunner:
    """Resolves a binding to argv and runs it, or explains why it can't."""

    def __init__(
        self,
        context: ActionContext,
        spawner: Spawner | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self._ctx = context
        self._spawn = spawner or _spawn
        self._which = which or shutil.which
        self._available: dict[str, bool] = {}
        self._warned: set[str] = set()

    def available(self, binary: str) -> bool:
        """Is this external command installed? Probed once, then cached."""
        if binary not in self._available:
            self._available[binary] = self._which(binary) is not None
        return self._available[binary]

    def argv_for(self, binding: str | list[str]) -> list[str] | None:
        """Full argv for a binding, or None if its command isn't installed."""
        if isinstance(binding, list):
            return list(binding) if self.available(binding[0]) else None
        spec = ACTIONS.get(binding)
        if spec is None:
            # Unreachable via config (validated at load), but a caller could
            # pass anything, and silently doing nothing would be worse.
            log.warning("no such action: %s", binding)
            return None
        return spec.build(self._ctx) if self.available(spec.binary) else None

    def run(self, binding: str | list[str]) -> bool:
        """Run a binding. Returns True if it was actually spawned."""
        argv = self.argv_for(binding)
        if argv is None:
            self._warn_missing(binding)
            return False
        log.info("run: %s", " ".join(argv))
        try:
            self._spawn(argv)
        except OSError as exc:
            log.warning("failed to run %s: %s", " ".join(argv), exc)
            return False
        return True

    def preflight(self, bindings: dict[str, str | list[str]]) -> None:
        """Report missing tools once at startup rather than on first press.

        Finding out that ``playerctl`` isn't installed should not require
        pressing a button and watching nothing happen.
        """
        for name, binding in sorted(bindings.items()):
            if self.argv_for(binding) is None:
                log.warning("binding %s disabled: %s not installed", name, _binary(binding))

    def _warn_missing(self, binding: str | list[str]) -> None:
        binary = _binary(binding)
        if binary and binary not in self._warned:
            self._warned.add(binary)
            log.warning("%s is not installed; that binding does nothing", binary)


def _binary(binding: str | list[str]) -> str | None:
    """The external command a binding depends on."""
    if isinstance(binding, list):
        return binding[0] if binding else None
    spec = ACTIONS.get(binding)
    return spec.binary if spec else None
