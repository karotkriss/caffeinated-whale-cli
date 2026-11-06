"""A branch, tag or commit field keeps what was typed, in a REAL browser.

The field is a frappe-ui Combobox, which puts its input back to the committed value when
its list closes, so a commit hash the list cannot show used to vanish on Tab and the
dialog stayed unconfirmable. Typing has to behave as it does in a plain text input, with
or without suggestions: nothing here depends on the repository being reachable.
"""

from __future__ import annotations

import json

import pytest

from . import harness
from .browser import open_signed_in
from .dialogs import DAEMON_TOKEN, PASSWORD, page, press, settled

pytestmark = [pytest.mark.e2e]

COMMIT = "0123abc"
_INPUT = "document.querySelector('[role=dialog] input[role=combobox]')"
_LIST_CLOSED = "!document.querySelector('[role=listbox]')"


@pytest.fixture()
def ui_with_instance(running_instance):
    with harness.ui_daemon(DAEMON_TOKEN) as base:
        yield base, running_instance.name


def _field_open(chrome, what: str) -> None:
    chrome.wait(f"!!{_INPUT}", desc=f"the ref field of {what}")
    settled(chrome)


def _type_and_leave(chrome, text: str) -> str:
    """Replace the field's text, Tab out of it, and return what the field then holds."""
    chrome.eval(
        f"(() => {{ const i = {_INPUT}; i.focus(); i.select();"
        f" return document.execCommand({'insertText' if text else 'delete'!r}, false,"
        f" {json.dumps(text)}) }})()"
    )
    chrome.press("Tab")
    chrome.wait(_LIST_CLOSED, desc="the ref list closed")
    return chrome.eval(f"{_INPUT}.value")


def _dialog_text(chrome) -> str:
    return chrome.eval("document.querySelector('[role=dialog]').textContent")


def _button_enabled(chrome, label: str) -> bool:
    return chrome.eval(
        f"(() => {{ const b = [...document.querySelectorAll('[role=dialog] button')]"
        f".find((e) => e.textContent.trim() === {json.dumps(label)}); return !!b && !b.disabled }})()"
    )


def test_ref_fields_keep_a_typed_commit(chrome, ui_with_instance):
    base, project = ui_with_instance
    open_signed_in(chrome, base, PASSWORD)
    chrome.viewport(1280, 800)
    page(chrome, base, f"/instances/{project}/benches/0", "Apps")
    press(chrome, "[role=tab]", "Apps")

    chrome.click("[aria-label='Actions for frappe']")
    press(chrome, "[role=menuitem]", "Check out ref")
    _field_open(chrome, "Check out ref")
    chrome.wait(
        "performance.getEntriesByType('resource')"
        ".some((e) => e.name.includes('/apps/refs?url='))",
        desc="the field asked for the refs of the app's remote",
    )
    assert not _button_enabled(chrome, "Check out ref")
    assert _type_and_leave(chrome, COMMIT) == COMMIT
    assert f"frappe {COMMIT}" in _dialog_text(chrome)
    assert _button_enabled(chrome, "Check out ref")
    # Emptying the field is an empty value again, not a stale one.
    assert _type_and_leave(chrome, "") == ""
    assert not _button_enabled(chrome, "Check out ref")
    assert COMMIT not in _dialog_text(chrome)
    chrome.press("Escape")
    chrome.wait("!document.querySelector('[role=dialog]')", desc="Check out ref closed")

    chrome.click("[aria-label='More actions']")
    press(chrome, "[role=menuitem]", "Install apps")
    _field_open(chrome, "Install apps")
    assert _type_and_leave(chrome, COMMIT) == COMMIT
