"""``core.failure_causes`` names the common Frappe/bench/git failures in plain words."""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core.failure_causes import explain


@pytest.mark.parametrize(
    ("output", "code", "phrase"),
    [
        (
            "MySQLdb.OperationalError: (1045, \"Access denied for user 'root'@'172.23.0.5' "
            '(using password: YES)")',
            "db.access_denied",
            "database user 'root'",
        ),
        (
            "pymysql.err.OperationalError: (2003, \"Can't connect to MySQL server on 'mariadb'\")",
            "db.unreachable",
            "could not be reached",
        ),
        (
            "(1007, \"Can't create database '_5e5a'; database exists\")",
            "db.exists",
            "'_5e5a'",
        ),
        ("Site site1.localhost already exists", "site.exists", "site1.localhost"),
        ("error: No space left on device", "disk.full", "ran out of space"),
        ("FATAL ERROR: JavaScript heap out of memory", "memory.exhausted", "out of memory"),
        (
            "fatal: Authentication failed for 'https://github.com/acme/app.git/'",
            "git.auth_failed",
            "https://github.com/acme/app.git/",
        ),
        (
            "fatal: could not read Username for 'https://github.com': No such device",
            "git.auth_failed",
            "https://github.com",
        ),
        (
            "fatal: repository 'https://github.com/acme/nope.git/' not found",
            "git.repo_not_found",
            "nope.git",
        ),
        ("fatal: Remote branch feat-x not found in upstream origin", "git.ref_not_found", "feat-x"),
        ("remote: ERROR: Repository not found.", "git.repo_not_found", "The git repository was"),
        ("Temporary failure in name resolution", "network.dns", "A host name could not"),
        (
            "fatal: unable to access: Could not resolve host: github.com",
            "network.dns",
            "github.com",
        ),
        ("ModuleNotFoundError: No module named 'hrms'", "python.module_missing", "'hrms'"),
        (
            "redis.exceptions.ConnectionError: Error 111 connecting to redis-cache:6379.",
            "redis.unreachable",
            "redis-cache:6379",
        ),
        ("OSError: [Errno 98] Address already in use", "port.in_use", "already in use"),
    ],
)
def test_known_shapes_are_named(output, code, phrase):
    cause = explain("Traceback (most recent call last):\n  ...\n" + output)
    assert cause is not None
    assert cause.code == code
    assert phrase in cause.text


def test_a_1045_wins_over_the_generic_shapes_in_the_same_output():
    output = (
        "Error 111 connecting to redis-cache:6379.\n"
        "MySQLdb.OperationalError: (1045, \"Access denied for user 'root'@'x'\")"
    )
    assert explain(output).code == "db.access_denied"


def test_an_earlier_more_specific_text_wins_over_a_later_wider_one():
    failing = "MySQLdb.OperationalError: (1045, \"Access denied for user 'root'@'x'\")"
    earlier_steps = "Could not resolve host: pypi.org\nError 111 connecting to redis-cache:6379."
    assert explain(failing, earlier_steps).code == "db.access_denied"
    assert explain(None, "", earlier_steps).code == "network.dns"


@pytest.mark.parametrize("output", [None, "", "Command exited 1", "Traceback: KeyError: 'x'"])
def test_unknown_output_is_none_never_a_guess(output):
    assert explain(output) is None
