"""Headless Chrome for the web UI proofs: the DevTools driver and the page sign-in.

Shared by every E2E that needs a real browser (page navigation, dialog layout). The
driver speaks the DevTools protocol over Chrome's ``--remote-debugging-pipe`` with the
stdlib only, so no driver package or browser download is part of the test deps; the
Chrome binary comes from the runner (``CWE2E_CHROME`` overrides the lookup).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import harness

CHROME = os.environ.get("CWE2E_CHROME") or next(
    (
        p
        for p in map(
            shutil.which, ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
        )
        if p
    ),
    None,
)


class Chrome:
    """Headless Chrome driven over ``--remote-debugging-pipe`` (fd 3 in, fd 4 out)."""

    def __init__(self, user_data_dir: Path):
        chrome_in_r, self._w = os.pipe()
        self._r, chrome_out_w = os.pipe()

        def _wire():  # runs in the child before exec: the pipes become fds 3 and 4
            a, b = os.dup(chrome_in_r), os.dup(chrome_out_w)
            os.dup2(a, 3)
            os.dup2(b, 4)

        self.proc = subprocess.Popen(
            [
                CHROME,
                "--headless=new",
                "--remote-debugging-pipe",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={user_data_dir}",
                "--window-size=1280,800",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=False,
            preexec_fn=_wire,
        )
        os.close(chrome_in_r)
        os.close(chrome_out_w)
        self._id = 0
        self._replies: dict[int, dict] = {}
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        target = next(
            t for t in self.send("Target.getTargets")["targetInfos"] if t["type"] == "page"
        )
        self.session = self.send(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )["sessionId"]

    def _read(self) -> None:
        buf = b""
        while True:
            chunk = os.read(self._r, 65536)
            if not chunk:
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                if "id" in msg:
                    with self._cv:
                        self._replies[msg["id"]] = msg
                        self._cv.notify_all()

    def send(
        self, method: str, params: dict | None = None, *, page: bool = False, timeout: float = 30
    ) -> dict:
        self._id += 1
        msg: dict = {"id": self._id, "method": method, "params": params or {}}
        if page:
            msg["sessionId"] = self.session
        os.write(self._w, json.dumps(msg).encode() + b"\0")
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._id not in self._replies:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"no reply to {method} within {timeout}s")
                self._cv.wait(left)
            reply = self._replies.pop(self._id)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    def eval(self, expression: str, *, timeout: float = 30):
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            page=True,
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "evaluate failed"))
        return result["result"].get("value")

    def wait(self, expression: str, *, timeout: float = 30, desc: str = "page condition"):
        # Each poll's evaluate is bounded on its own: a reply lost around a page load (seen
        # once right after the first navigation) is retried within the wait instead of
        # consuming it whole.
        def poll():
            try:
                return self.eval(expression, timeout=5)
            except TimeoutError:
                return False

        return harness.wait_until(poll, timeout=int(timeout), interval=0.1, desc=desc)

    def stable_point(self, selector: str) -> dict:
        """The element's centre once it stays put: its box unchanged across two consecutive
        animation frames, and no view transition running. A press aimed at a box that is
        still moving, or at a page still being cross-faded, would measure the test's timing
        instead of the page."""
        got = self.eval(
            f"""(async () => {{
  const el = document.querySelector({json.dumps(selector)})
  if (!el) return {{ error: 'nothing matches' }}
  el.scrollIntoView({{ block: 'center' }})
  const frame = () => new Promise((r) => requestAnimationFrame(r))
  const until = performance.now() + 10000
  let last = null
  while (performance.now() < until) {{
    await frame()
    const r = el.getBoundingClientRect()
    const box = [r.left, r.top, r.width, r.height].join()
    if (box === last && el.isConnected && !document.activeViewTransition)
      return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }}
    last = box
  }}
  return {{ error: 'not still within 10 s', box: last, transition: !!document.activeViewTransition }}
}})()"""
        )
        assert "error" not in got, f"{selector!r}: {got}"
        return got

    def press_at(self, point: dict) -> None:
        """A real mouse press and release at ``point`` (reka-ui tabs switch on pointerdown,
        which a synthetic ``click()`` never produces)."""
        for kind in ("mousePressed", "mouseReleased"):
            self.send(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
                page=True,
            )

    def click(self, selector: str) -> None:
        self.press_at(self.stable_point(selector))

    def press(self, key: str) -> None:
        """One key press (``Escape`` closes the topmost dialog)."""
        code = {"Escape": 27}[key]
        for kind in ("keyDown", "keyUp"):
            self.send(
                "Input.dispatchKeyEvent",
                {"type": kind, "key": key, "code": key, "windowsVirtualKeyCode": code},
                page=True,
            )

    def viewport(self, width: int, height: int) -> None:
        """Emulate a window of ``width`` x ``height`` CSS pixels (mobile below 640)."""
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 640},
            page=True,
        )

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", {"url": url}, page=True)

    def screenshot(self, path: Path, *, clip: dict | None = None) -> None:
        """Save a PNG of the viewport, or of ``clip`` (``{x, y, width, height}``) only."""
        import base64

        params: dict = {"format": "png"}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        data = self.send("Page.captureScreenshot", params, page=True)["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.send("Browser.close", timeout=5)
        except Exception:  # noqa: BLE001 - best effort, the kill below is the guarantee
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def sign_in(base: str, password: str) -> tuple[str, str]:
    """Sign the admin account in the way the page does and return the session cookie
    (name, value), with its password set to ``password``.

    The account is reset to the default password first, because the session's
    ``CWCLI_HOME`` is shared and an earlier sign-in may have changed it.
    """
    reset = harness.run_cwcli("admin", "reset-password")
    assert reset.returncode == 0, reset.stderr
    req = urllib.request.Request(
        base + "/api/v1/session",
        data=json.dumps({"username": "admin", "password": "admin", "remember": False}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - loopback
        name, value = resp.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)
    req = urllib.request.Request(
        base + "/api/v1/session/password",
        data=json.dumps({"new": password, "current": "admin"}).encode(),
        headers={"Content-Type": "application/json", "Cookie": f"{name}={value}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - loopback
        assert resp.status == 200
    return name, value


def open_signed_in(chrome: Chrome, base: str, password: str) -> None:
    """Put the browser's session cookie in place so the next navigation lands on a page."""
    chrome.send("Network.enable", page=True)
    name, value = sign_in(base, password)
    chrome.send("Network.setCookie", {"name": name, "value": value, "url": base}, page=True)
