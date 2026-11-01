"""Dialog content stays inside the dialog, in a REAL browser, at desktop and phone widths.

Settings > Integrations used to grow wider than its modal once the Remote hosts over
SSH card showed the hub's public key and its ``cwcli remote allow`` command: frappe-ui's
tab panel is a flex-row item with no ``min-width: 0``, so one unbreakable line set the
panel's width, the dialog clipped the right edge, the Integrations intro lost its last
words and the cards' buttons ("Settings…", "Add from a repository…") were cut off. On a
phone the tab root was wider than the dialog from the start (``w-screen`` inside a dialog
with margins). The fix is shared dialog layout (``webui/src/style.css``, the local
``SettingsRow``/``SettingsHeader`` wrappers), not one card.

Only a browser can measure that, so this drives headless Chrome against the shipped
``cwcli ui`` daemon, seeds real long values where the page shows user data (the hub key
and allow command, a long search path, a long app source, a long remote host address,
long browser user agents), opens every Settings pane and every dialog reachable without
an instance, and fails when any element inside the topmost dialog lies past the
dialog's horizontal edges or when a control is clipped by an inner scroll region.
Each pane and dialog is measured twice: as served, and again with an unbreakable
token appended to its text so a long value anywhere in it is covered, not only where
the seeds land. The instance-backed dialogs (new bench, new site, clone or move sites,
stop, clear cache) are measured in ``test_webui_overflow_instance_dialogs_e2e.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request

import pytest

from . import harness
from .browser import open_signed_in
from .dialogs import (
    DAEMON_TOKEN,
    PASSWORD,
    SIZES,
    check,
    close_dialog,
    open_dialog,
    page,
    panel_shows,
    press,
    settings,
    settled,
)

pytestmark = [pytest.mark.e2e]

# Real long values, each where the page shows user data; none has a break opportunity.
LONG_PATH = "/workspace/" + "benches-with-an-unusually-long-directory-name-" * 3 + "0123456789"
LONG_SOURCE = "github:" + "an-organisation-with-an-unusually-long-name-" * 2 + "0123456789"
LONG_ADDRESS = "a-remote-machine-with-an-unusually-long-hostname-label.internal.example.invalid"
# The daemon keeps the first 80 characters of a user agent, so the marker leads.
LONG_AGENT = (
    "ExampleCorporateBrowserBuild/2026.11.01.0123456789 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
# Every Settings section in sidebar order: (section, tab label, what shows once its data is in).
PANES = [
    ("paths", "Search paths", LONG_PATH),
    ("cache", "Cache", "Remembered projects"),
    ("daemons", "Background daemons", "Persistent credential bridge"),
    ("sources", "App sources", LONG_SOURCE),
    ("agents", "Agent hooks", "Install or repair"),
    ("integrations", "Integrations", "Hub key: ssh-ed25519"),
    ("hosts", "Remote hosts", LONG_ADDRESS),
    ("account", "Your account", "ExampleCorporateBrowserBuild"),
    ("appearance", "Appearance", "Appearance"),
    ("health", "Health check", " passed"),
    ("about", "About cwcli", "Config file"),
]

# Dialogs reachable without an instance: (where, how to open it, its title's start).
# `where` is a Settings section or a page route.
DIALOGS = [
    ("integrations", ("button", "Settings…"), "Remote hosts over SSH"),
    ("integrations", ("button", "Turn off"), "Turn off Remote hosts over SSH"),
    ("integrations", ("button", "Add from a repository"), "Add a community integration"),
    ("hosts", ("button", "Add host"), "Add a remote host"),
    ("hosts", ('button[aria-label^="Remove "]', None), "Remove"),
    ("about", ("button", "Update"), "Update cwcli"),
    ("/instances", ("button", "New instance"), "Create an instance"),
    ("/instances", ("button", "Appearance"), "Appearance"),
    ("/schedules", ("button", "New schedule"), "New schedule"),
    ("/schedules", ("button", "Windows and guards"), "Windows and guards"),
    ("/backups", ("button", "Receive"), "Receive a backup"),
    ("/backups", ("button", "Retention and schedule"), "Backup retention"),
    ("/backups", ("button", "Back up a site"), "Back up"),
]


def _api(base: str, path: str, payload: dict) -> dict:
    status, body = harness.api_call(base, path, payload, token=DAEMON_TOKEN)
    assert status == 200, f"POST {path} -> {status}: {body}"
    return body["data"]


def _seed(base: str, tmp_path) -> dict:
    """Long values where Settings shows user data; returns what to undo."""
    data = _api(
        base,
        "/api/v1/integrations",
        {
            "op": "configure",
            "id": "remote_ssh",
            "settings": {"hub_name": "hub-example"},
            "secrets": {},
            "enable": True,
        },
    )
    assert data["integration"]["state"] == "on", data
    notices = " ".join(data["integration"]["notices"])
    assert "Hub key: ssh-ed25519" in notices and "cwcli remote allow --hub" in notices, notices
    _api(base, "/api/v1/settings", {"setting": "paths.add", "path": LONG_PATH})
    _api(base, "/api/v1/settings", {"setting": "catalog.source_add", "source": LONG_SOURCE})
    keygen = shutil.which("ssh-keygen")
    assert keygen, "ssh-keygen is needed to make a placeholder host key"
    key = tmp_path / "placeholder_host_key"
    subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    host_key = " ".join(key.with_suffix(".pub").read_text().split()[:2])
    host = _api(
        base,
        "/api/v1/hosts",
        {
            "op": "add",
            "name": "build box",
            "address": LONG_ADDRESS,
            "user": "deploy",
            "port": 22,
            "host_key": host_key,
        },
    )["host"]
    return {"host": host["id"]}


def _unseed(base: str, seeded: dict) -> None:
    """The session home is shared with the other standalone tests."""
    _api(base, "/api/v1/hosts", {"op": "remove", "id": seeded["host"]})
    _api(base, "/api/v1/settings", {"setting": "catalog.source_remove", "source": LONG_SOURCE})
    _api(base, "/api/v1/settings", {"setting": "paths.remove", "path": LONG_PATH})
    _api(base, "/api/v1/integrations", {"op": "disable", "id": "remote_ssh"})


def _seed_sessions(base: str) -> None:
    """Two more browsers signed in, each with a long user agent, for Your account."""
    for suffix in ("desktop", "phone"):
        req = urllib.request.Request(
            base + "/api/v1/session",
            data=json.dumps({"username": "admin", "password": PASSWORD, "remember": True}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": f"{LONG_AGENT}-{suffix}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - loopback
            assert resp.status == 200


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    """The web UI daemon alone, with the long values seeded for the whole module."""
    with harness.ui_daemon(DAEMON_TOKEN) as base:
        seeded = _seed(base, tmp_path_factory.mktemp("cwe2e-dialog-seed"))
        try:
            yield base
        finally:
            _unseed(base, seeded)


@pytest.mark.standalone
@pytest.mark.parametrize("width, height", SIZES)
def test_settings_panes_and_dialogs_fit_the_dialog(chrome, ui, width, height):
    open_signed_in(chrome, ui, PASSWORD)
    _seed_sessions(ui)
    chrome.viewport(width, height)
    failures: list[str] = []

    # Every pane, by its tab, in one open modal.
    chrome.navigate(f"{ui}/settings/{PANES[0][0]}")
    chrome.wait("document.querySelectorAll('[role=tab]').length >= 11", desc="the Settings modal")
    settled(chrome)
    for _section, label, shows in PANES:
        press(chrome, "[role=tab]", label)
        chrome.wait(panel_shows(shows), desc=f"the {label} pane with its data")
        failures += check(chrome, f"{width}px {label} pane")

    # Every dialog reachable without an instance.
    for where, opener, title in DIALOGS:
        what = f"{width}px {title!r} dialog"
        if where.startswith("/"):
            page(chrome, ui, where, opener[1])
        else:
            settings(chrome, ui, where, dict((s, t) for s, _, t in PANES)[where])
        open_dialog(chrome, opener, title, what)
        failures += check(chrome, what)
        close_dialog(chrome, what)

    assert not failures, "\n".join(failures)
