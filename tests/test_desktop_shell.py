import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "desktop" / "src-tauri"
CONFIG = MANIFEST_DIR / "tauri.conf.json"


def test_the_configured_splash_exists_and_is_tracked():
    config = json.loads(CONFIG.read_text())
    splash = (MANIFEST_DIR / config["build"]["frontendDist"] / "index.html").resolve()

    assert splash == ROOT / "desktop" / "dist" / "index.html"
    assert splash.is_file()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", splash.relative_to(ROOT).as_posix()],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr
