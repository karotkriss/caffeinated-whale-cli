"""Start enabled background daemons that should be up but are not.

An install that is enabled with a start-at-boot unit has told cwcli it wants the
daemon running; if it is not (the boot unit was broken, the box rebooted, an
upgrade replaced the code under it) ``recover`` brings it back, or says plainly
why it cannot. Each daemon is attempted on its own: one failure never hides the
other, and nothing is raised - callers (``cwcli ui`` and the web daemon's start)
print the notes and carry on.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import auto_inspect, cred_bridge
from .errors import CwcliError


@dataclass(frozen=True, slots=True, kw_only=True)
class Recovery:
    daemon: str  # display name
    started: bool
    note: str  # one line: what happened, or why it could not be started


def recover() -> list[Recovery]:
    """Start every daemon that is enabled with start-at-boot on but not running."""
    notes: list[Recovery] = []
    for name, module, start_cmd in (
        ("auto-inspect", auto_inspect, "cwcli config auto-inspect enable"),
        ("credential bridge", cred_bridge, "cwcli config cred-bridge start"),
    ):
        try:
            state = module.status().data
            if state is None or not (
                state.enabled and state.startup_enabled and not state.daemon_running
            ):
                continue
            module.start()
        except CwcliError as e:
            notes.append(
                Recovery(
                    daemon=name,
                    started=False,
                    note=f"{name} is enabled but stopped and could not be started: "
                    f"{e.message} (try `{start_cmd}`)",
                )
            )
        except Exception as e:  # a recovery attempt must never break the caller
            notes.append(
                Recovery(
                    daemon=name,
                    started=False,
                    note=f"{name} is enabled but stopped and could not be started: {e} "
                    f"(try `{start_cmd}`)",
                )
            )
        else:
            notes.append(
                Recovery(daemon=name, started=True, note=f"{name} was stopped; started it")
            )
    return notes
