"""Real-Docker E2E (steering 041): a site whose database user is scoped to an address
the frappe container no longer has is detected and repaired.

Frappe creates each site's MariaDB user host-scoped to the client it saw at
``bench new-site`` - in Docker the frappe container's IP - so a restart that moves the
container to a new IP refuses every login with ``Access denied for user
'_<hash>'@'<new ip>'`` and the site answers a bare 500 (seen on a staging server, 2026-10-28).
This plants that shape on the session site deterministically (the user renamed to an
address nobody has), shows the symptom, and proves ``cwcli db-access check`` names it on
both surfaces and ``cwcli db-access repair --yes`` makes it ``'<user>'@'%'`` so the site
answers again. The original grant rows are put back when the test ends.
"""

from __future__ import annotations

import json
import re
import shlex

import pytest

from . import harness

pytestmark = pytest.mark.e2e

WRONG_HOST = "172.99.0.9"


def _root_sql(inst, sql: str) -> str:
    # The root password cwcli set at init, read the way cwcli reads it (never printed).
    from caffeinated_whale_cli.core import db_password

    pw = db_password.resolve(inst.name, None)
    script = (
        "C=$(command -v mariadb || command -v mysql); "
        f"MYSQL_PWD={shlex.quote(pw)} $C -h mariadb -u root --batch --skip-column-names -e {shlex.quote(sql)}"
    )
    code, out = harness.exec_in_frappe(inst.name, script, workdir=inst.bench)
    assert code == 0, f"root SQL failed: {out}"
    return out


def _site_user(inst) -> str:
    code, out = harness.exec_in_frappe(
        inst.name, f"cat sites/{shlex.quote(inst.site)}/site_config.json", workdir=inst.bench
    )
    assert code == 0, out
    cfg = json.loads(out)
    return cfg.get("db_user") or cfg["db_name"]


def test_a_host_scoped_site_user_is_detected_and_repaired(running_instance):
    inst = running_instance
    user = _site_user(inst)
    q = user.replace("'", "''")
    before = [
        line.split("\t")[1]
        for line in _root_sql(
            inst, f"SELECT user, host FROM mysql.user WHERE user='{q}'"
        ).splitlines()
        if "\t" in line
    ]
    assert before, "the session site has no database user?"
    try:
        # Plant the staging-server shape: every grant row scoped to an address nobody has.
        for h in before:
            _root_sql(inst, f"RENAME USER '{q}'@'{h}' TO '{q}'@'{WRONG_HOST}'; FLUSH PRIVILEGES")
            break  # one row suffices; MariaDB refuses duplicate targets
        check = harness.run_cwcli("db-access", "check", inst.name, "--bench", "0")
        text = harness.collapse_ws(harness.strip_ansi(check.stdout + check.stderr))
        assert check.returncode == 1, text
        assert inst.site in text and "REFUSED" in text and WRONG_HOST in text, text

        axi = harness.run_cwcli("axi", "db-access", "check", inst.name, "--bench", "0")
        assert axi.returncode == 1, axi.stdout + axi.stderr
        assert "ok: false" in axi.stdout and inst.site in axi.stdout, axi.stdout

        repair = harness.run_cwcli("db-access", "repair", inst.name, "--bench", "0", "--yes")
        text = harness.collapse_ws(harness.strip_ansi(repair.stdout + repair.stderr))
        assert repair.returncode == 0, text
        assert "(repaired)" in text and "ok" in text, text
        after = [
            line.split("\t")[1]
            for line in _root_sql(
                inst, f"SELECT user, host FROM mysql.user WHERE user='{q}'"
            ).splitlines()
            if "\t" in line
        ]
        assert "%" in after and WRONG_HOST not in after, after

        # The site answers again through Frappe itself.
        code, out = harness.exec_in_frappe(
            inst.name,
            f"bench --site {shlex.quote(inst.site)} execute frappe.ping",
            workdir=inst.bench,
        )
        assert code == 0 and "pong" in out, out
        ok = harness.run_cwcli("db-access", "check", inst.name, "--bench", "0")
        assert ok.returncode == 0, ok.stdout + ok.stderr
    finally:
        # Put the original grant rows back exactly (a '%' row stays '%').
        now = [
            line.split("\t")[1]
            for line in _root_sql(
                inst, f"SELECT user, host FROM mysql.user WHERE user='{q}'"
            ).splitlines()
            if "\t" in line
        ]
        if before and now and set(now) != set(before):
            src = now[0]
            if before[0] != src:
                _root_sql(
                    inst, f"RENAME USER '{q}'@'{src}' TO '{q}'@'{before[0]}'; FLUSH PRIVILEGES"
                )
    final = [
        line.split("\t")[1]
        for line in _root_sql(
            inst, f"SELECT user, host FROM mysql.user WHERE user='{q}'"
        ).splitlines()
        if "\t" in line
    ]
    assert set(final) == set(before), (before, final)
    assert re.match(r"^\S+$", user)
