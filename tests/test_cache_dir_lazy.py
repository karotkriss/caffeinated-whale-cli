"""The cache directory is created on first actual cache use, never at import time.

Each case runs a fresh ``cwcli`` process against a throwaway ``CWCLI_HOME``, which
is exactly how a real invocation resolves the cache location.
"""

import os
import stat
import subprocess
import sys

import pytest

from caffeinated_whale_cli.core import init, version


def _run(home, *code_or_args, module=False):
    env = dict(os.environ, CWCLI_HOME=str(home), CWCLI_NO_UPDATE_CHECK="1")
    env.pop("CWCLI_NO_CACHE", None)
    cmd = (
        [sys.executable, "-c", code_or_args[0]]
        if module
        else [
            sys.executable,
            "-c",
            "from caffeinated_whale_cli.main import cli; cli()",
            *code_or_args,
        ]
    )
    return subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)


@pytest.mark.parametrize("args", [("--version",), ("config", "cache", "path")])
def test_read_only_command_creates_no_cache_dir(tmp_path, args):
    home = tmp_path / "home"
    _run(home, *args)
    assert not (home / "cache").exists()


def test_importing_the_cli_creates_nothing(tmp_path):
    home = tmp_path / "home"
    _run(home, "import caffeinated_whale_cli.main", module=True)
    assert not (home / "cache").exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix permissions not supported on Windows")
def test_first_cache_use_creates_dir_0700(tmp_path):
    home = tmp_path / "home"
    _run(
        home,
        "from caffeinated_whale_cli.utils import db_utils; db_utils.initialize_database()",
        module=True,
    )
    assert stat.S_IMODE((home / "cache").stat().st_mode) == 0o700
    assert (home / "cache" / "cwc-cache.db").is_file()


@pytest.mark.skipif(os.name == "nt", reason="Unix permissions not supported on Windows")
@pytest.mark.parametrize(
    "write",
    [
        lambda: version._record_attempt("1.0.0"),
        lambda: init._write_bench_tag_cache("v5.0.0"),
    ],
    ids=["version-check", "bench-tag"],
)
def test_side_cache_first_creating_the_dir_creates_it_0700(tmp_path, monkeypatch, write):
    home = tmp_path / "home"
    monkeypatch.setenv("CWCLI_HOME", str(home))
    old_umask = os.umask(0o022)
    try:
        write()
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE((home / "cache").stat().st_mode) == 0o700
    assert len(list((home / "cache").iterdir())) == 1
