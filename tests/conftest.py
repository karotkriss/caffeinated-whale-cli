"""Top-level pytest config for the two-tier suite.

The suite is split into a fast ``unit`` tier (no Docker) and a real-Docker
``e2e`` / ``e2e_p2p`` tier (``tests/e2e/``). Rather than hand-mark every legacy
test, this hook auto-applies the ``unit`` marker to any collected test that is
not already marked ``e2e`` / ``e2e_p2p``. That way:

- a bare ``pytest`` (default ``-m "not e2e and not e2e_p2p"``) runs the unit tier,
- the unit CI job (``-m unit``) selects the same set, and
- ``-m e2e`` selects only the real-Docker tier.

During the parallel-run migration off the mock suite, the legacy container-mock
tests are carried in the ``unit`` tier alongside the permanent mock-free
pure-logic tests; they are retired per command as each command's real E2E lands
(see ``openspec/changes/rebuild-e2e-test-suite``). The end state is a ``unit``
tier of only mock-free pure-logic tests.
"""


def pytest_collection_modifyitems(config, items):
    import pytest

    for item in items:
        if item.get_closest_marker("e2e") or item.get_closest_marker("e2e_p2p"):
            continue
        item.add_marker(pytest.mark.unit)
