"""Real-Docker E2E for multibench ``status``/``start`` against TWO GENUINELY
SERVING benches, each on its own assigned port.

This is the runtime half of ``report-status-per-bench``. The structural half is
covered by ``test_start_status_new_behavior_e2e.py``, whose second bench is a
DIRECTORY SKELETON - no virtualenv, no Procfile, no supervisord, nothing bound to
a port. A skeleton can prove the document SHAPE (two benches reported, each named,
an unreadable port reported unknown) and it proves nothing at all about the probe,
because the defects the change exists to remove only exist when a second bench is
genuinely answering on a port of its own:

  - **F3** a fully healthy bench past the first reported ``degraded``, because the
    probe was hardcoded to ``:8000`` and therefore measured bench 0's web server.
  - **F4** a bench serving NOTHING reported its neighbour's live HTTP code, by the
    same mechanism in the other direction.
  - **F5** ``cwcli start --bench N`` for N>0 sat on ``:8000`` for the full 60-second
    wait and then warned that a bench which had been serving the whole time had
    failed to start.

So this module builds a real second bench: its own ``bench init`` virtualenv, its
own site, its own Procfile, its own supervisord, and the port bench's own
``make_ports`` assigned it.

The same fixture also backs the ``cwcli axi url`` discriminator tests near the
bottom of this file: that verb's whole reason to exist is the SAME class of bug
(a probe that reads one bench's port while reporting on another), so its E2E
proof reuses this module's already-built two-genuinely-serving-benches instance
rather than paying for a second one.

**Every test asserts the positive before the negative.** Before asserting that a
bench is not misreported it proves that bench is genuinely serving, because a
"no wrong answer" check passes just as happily against a bench that is not there.
F3, F4, and F5 also assert the exact DISCRIMINATOR: the value the removed
bench-blind probe would have read, shown to differ from the correct target.

Multibench port assignment and the probe are version-agnostic, so this runs once
on the v16 leg (the ``v16_only`` precedent from ``test_scale_e2e``). It builds and
tears down its own instance, so it is ``standalone``; the two bench builds are the
cost, paid once for the module.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="multibench port assignment and the web probe are version-agnostic; v16 leg only",
)

FIRST_BENCH_PATH = harness.DEFAULT_BENCH_PATH
FIRST_SITE = harness.DEFAULT_SITE
# Deliberately sorts before the original bench. This is the live ordering that
# used to renumber the original from 0 to 1 as soon as the add completed.
SECOND_BENCH_NAME = "aaa-bench"
SECOND_BENCH_PATH = f"/workspace/{SECOND_BENCH_NAME}"
SECOND_SITE = "second.localhost"

# A scalar TOON key. Guards the block parser against a nested table's CSV rows,
# whose display labels can carry a colon (`worker:default`) and would otherwise
# parse as a key/value pair.
_SCALAR_KEY = re.compile(r"[a-z_]+")


# --------------------------------------------------------------------------- #
# Reading real state out of the instance
# --------------------------------------------------------------------------- #
def _assigned_web_port(project: str, bench_path: str) -> int:
    """The port THIS bench serves inside the container, from bench's own config.

    ``sites/common_site_config.json`` is the port truth: bench's ``make_ports``
    writes it and cwcli writes zero port config, so this is the same source
    ``status`` reads - and reading it here is what lets the tests below assert
    that the two benches genuinely differ.
    """
    code, out = harness.exec_in_frappe(project, f"cat {bench_path}/sites/common_site_config.json")
    assert code == 0, out
    return int(json.loads(out)["webserver_port"])


def _http_code(project: str, port: int, site: str | None = None) -> str:
    """The HTTP code a request to ``port`` gets inside the container.

    ``"000"`` is curl's own answer for "nothing answered", so a dead port and a
    live one are distinguishable without a second mechanism. With ``site`` the
    request carries a ``Host`` header, which is what a real user's request looks
    like against multi-tenant Frappe (a host-less one is answered 404 by a
    perfectly healthy bench).
    """
    host = f'-H "Host: {site}" ' if site else ""
    _code, out = harness.exec_in_frappe(
        project,
        f'curl -s --max-time 10 -o /dev/null -w "%{{http_code}}" {host}http://localhost:{port}',
    )
    lines = [line for line in out.strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else "000"


def _wait_code(project: str, port: int, expected: str, site: str | None = None, timeout: int = 300):
    harness.wait_until(
        lambda: _http_code(project, port, site) == expected,
        timeout=timeout,
        interval=3,
        desc=f"{project} :{port} answering {expected!r}",
    )


def _bench_blocks(toon: str, *, key_field: str = "bench_path") -> dict[str, dict[str, str]]:
    """Every ``benches[...]`` item of a TOON document (``axi status``/``axi
    inspect``), keyed by ``key_field`` (``bench_path`` for status, ``path`` for
    inspect - each verb names the field differently).

    Each bench is a ``- key: value`` TOON item whose nested tables (``processes``
    for status, ``sites`` for inspect) sit deeper. Only the item's own scalar
    fields are collected, and quoted values (``web_http_code: "200"``) are
    unquoted, so a caller compares against the value rather than the encoder's
    quoting.
    """
    blocks: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    field_indent = -1
    in_benches = False
    for raw in toon.splitlines():
        if raw.startswith("benches["):
            in_benches = True
            continue
        if not in_benches:
            continue
        if raw.strip() and not raw.startswith(" "):
            break  # a top-level key: the bench list has ended
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if stripped.startswith("- "):
            current = {}
            field_indent = indent + 2
            stripped = stripped[2:]
        elif indent != field_indent:
            continue  # a nested table's rows sit deeper than the item's own fields
        if current is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not _SCALAR_KEY.fullmatch(key):
            continue
        value = value.strip().strip('"')
        current[key] = value
        if key == key_field:
            blocks[value] = current
    return blocks


def _axi_status_blocks(project: str, *args: str) -> tuple[str, dict[str, dict[str, str]]]:
    res = harness.run_cwcli("axi", "status", project, *args)
    assert res.returncode == 0, res.stdout + res.stderr
    out = harness.strip_ansi(res.stdout)
    return out, _bench_blocks(out)


def _bench_indices(project: str) -> dict[str, int]:
    """Each bench path's ``--bench`` index, read from the discovery verb itself.

    Indices are durable path identities. Reading them here rather than hardcoding
    them means the tests below name the bench they mean, including when display
    order differs from identity assignment order.
    """
    res = harness.run_cwcli("axi", "benches", project)
    assert res.returncode == 0, res.stdout + res.stderr
    indices: dict[str, int] = {}
    seen_header = False
    for line in harness.strip_ansi(res.stdout).splitlines():
        if line.startswith("benches["):
            seen_header = True
            continue
        if not seen_header or not line.startswith(" "):
            continue
        parts = [p.strip() for p in line.strip().split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            indices[parts[1]] = int(parts[0])
    return indices


# --------------------------------------------------------------------------- #
# The two-serving-bench fixture
# --------------------------------------------------------------------------- #
@dataclass
class TwoBenches:
    name: str
    port: int
    first_index_before_add: int
    first_index: int
    second_index: int
    first_port: int
    second_port: int


def _serve(inst: TwoBenches, index: int, port: int, site: str) -> None:
    """Bring one bench's stack up (idempotent) and wait for it to genuinely serve."""
    if _http_code(inst.name, port, site) == "200":
        return
    res = harness.run_cwcli("start", inst.name, "--bench", str(index), "--yes")
    assert res.returncode == 0, res.stdout + res.stderr
    _wait_code(inst.name, port, "200", site)


def _serve_both(inst: TwoBenches) -> None:
    """The shared precondition: BOTH benches genuinely serving their own sites."""
    _serve(inst, inst.first_index, inst.first_port, FIRST_SITE)
    _serve(inst, inst.second_index, inst.second_port, SECOND_SITE)


@pytest.fixture(scope="module")
def two_benches(port_allocator):
    """One instance holding TWO real benches, each genuinely serving its own site.

    Both are built by real ``cwcli init`` runs - the second one adds a bench to the
    already-running instance, which is the ordinary way a developer grows one - so
    each has its own virtualenv, apps, site, Procfile and supervisord, and each
    serves the port bench's own ``make_ports`` assigned it.
    """
    harness.enforce_isolation()
    name = harness.project_name("mbserve")
    port = port_allocator.next()
    common = (
        "--port",
        str(port),
        "--frappe-branch",
        harness.FRAPPE_BRANCH,
        "--admin-password",
        SESSION_ADMIN_PW,
        "--auto-start",
    )
    try:
        first = harness.run_cwcli("init", name, *common, timeout=harness.INIT_TIMEOUT)
        assert first.returncode == 0, first.stdout + first.stderr
        harness.wait_for_site_ready(name, FIRST_SITE, bench=FIRST_BENCH_PATH)

        initial_inspect = harness.run_cwcli("inspect", name, "--update")
        assert initial_inspect.returncode == 0, initial_inspect.stdout + initial_inspect.stderr
        initial_indices = _bench_indices(name)
        assert set(initial_indices) == {FIRST_BENCH_PATH}, initial_indices
        first_index_before_add = initial_indices[FIRST_BENCH_PATH]

        # The SECOND real bench, added to the running instance. Re-running init
        # against a live project is the supported way to add a bench (its own
        # ports are skipped by the port check), and it provisions and starts the
        # new bench exactly as the first one was.
        second = harness.run_cwcli(
            "init",
            name,
            *common,
            "--bench",
            SECOND_BENCH_NAME,
            "--site",
            SECOND_SITE,
            timeout=harness.INIT_TIMEOUT,
        )
        assert second.returncode == 0, second.stdout + second.stderr
        harness.wait_for_site_ready(name, SECOND_SITE, bench=SECOND_BENCH_PATH)

        # init clears the cache, so re-populate it with BOTH benches; --update
        # forces the full re-scan that discovers a brand-new bench.
        insp = harness.run_cwcli("inspect", name, "--update")
        assert insp.returncode == 0, insp.stdout + insp.stderr

        indices = _bench_indices(name)
        assert FIRST_BENCH_PATH in indices and SECOND_BENCH_PATH in indices, indices
        inst = TwoBenches(
            name=name,
            port=port,
            first_index_before_add=first_index_before_add,
            first_index=indices[FIRST_BENCH_PATH],
            second_index=indices[SECOND_BENCH_PATH],
            first_port=_assigned_web_port(name, FIRST_BENCH_PATH),
            second_port=_assigned_web_port(name, SECOND_BENCH_PATH),
        )
        # THE fixture precondition. Every assertion in this module distinguishes
        # one bench's port from the other's; if bench ever assigned both benches
        # the same port, every test here would pass while proving nothing.
        assert inst.first_port != inst.second_port, (
            f"the two benches must serve DIFFERENT ports, got {inst.first_port} "
            f"for both - every test in this module would be vacuous"
        )
        _serve_both(inst)
        yield inst
    finally:
        harness.cwcli_rm(name)


# --------------------------------------------------------------------------- #
# 0. bench identity survives an earlier-sorting add
# --------------------------------------------------------------------------- #
@v16_only
def test_prior_bench_identity_still_resolves_after_an_earlier_add(two_benches):
    inst = two_benches
    assert SECOND_BENCH_PATH < FIRST_BENCH_PATH

    # Positive first: the identity captured before the add still resolves to the
    # original bench through the real agent command and its live status probe.
    out, blocks = _axi_status_blocks(inst.name, "--bench", str(inst.first_index_before_add))
    assert FIRST_BENCH_PATH in blocks, out
    assert blocks[FIRST_BENCH_PATH]["bench_path"] == FIRST_BENCH_PATH, out
    assert inst.first_index == inst.first_index_before_add

    # The earlier bench exists, but the prior identity did not silently retarget it.
    assert SECOND_BENCH_PATH not in blocks, out


# --------------------------------------------------------------------------- #
# 1. the fixture itself: two benches, genuinely serving, on different ports
# --------------------------------------------------------------------------- #
@v16_only
def test_both_benches_genuinely_serve_on_their_own_ports(two_benches):
    """The baseline every test below stands on, asserted rather than assumed.

    Two benches, two sites, two ports, both answering 200 for their own site - and
    neither answering 200 for its NEIGHBOUR's site, which is what proves they are
    separate servers rather than one server behind two port numbers.
    """
    inst = two_benches
    _serve_both(inst)

    assert _http_code(inst.name, inst.first_port, FIRST_SITE) == "200"
    assert _http_code(inst.name, inst.second_port, SECOND_SITE) == "200"
    # Separate benches, not one bench routing two vhosts: neither bench holds the
    # other's site, so neither serves it. (Frappe's exact refusal code for an
    # unknown Host is its own business; that it is not a 200 is the claim.)
    assert _http_code(inst.name, inst.first_port, SECOND_SITE) != "200"
    assert _http_code(inst.name, inst.second_port, FIRST_SITE) != "200"

    # And status attributes each code to the bench, port and site it came from.
    out, blocks = _axi_status_blocks(inst.name)
    assert out.splitlines()[0] == "overall: running", out
    assert set(blocks) == {FIRST_BENCH_PATH, SECOND_BENCH_PATH}, out
    first, second = blocks[FIRST_BENCH_PATH], blocks[SECOND_BENCH_PATH]
    assert first["web_port"] == str(inst.first_port), out
    assert second["web_port"] == str(inst.second_port), out
    assert first["web_site"] == FIRST_SITE and second["web_site"] == SECOND_SITE, out
    assert first["web_http_code"] == "200" and second["web_http_code"] == "200", out
    assert first["overall"] == "running" and second["overall"] == "running", out


# --------------------------------------------------------------------------- #
# F3. a healthy bench past the first is NOT reported degraded
# --------------------------------------------------------------------------- #
@v16_only
def test_f3_a_healthy_second_bench_is_not_reported_degraded(two_benches):
    """The headline misreport: only the second bench is up, and it is healthy.

    This is the ordinary "I only started the bench I am working on" flow. With the
    bench-blind probe it produced a self-contradictory document - every process
    ``RUNNING`` beside a null web code - and an aggregate of ``degraded`` for a
    bench with nothing wrong with it.
    """
    inst = two_benches
    _serve_both(inst)
    try:
        stop = harness.run_cwcli("stop", inst.name, "--bench", str(inst.first_index))
        assert stop.returncode == 0, stop.stdout + stop.stderr
        _wait_code(inst.name, inst.first_port, "000")

        # POSITIVE FIRST: the second bench genuinely serves its own site on its
        # own port. Without this the assertions below would pass against a bench
        # that was never up.
        assert _http_code(inst.name, inst.second_port, SECOND_SITE) == "200"
        # THE DISCRIMINATOR: the first bench's port - what the removed probe
        # hardcoded - now answers nothing at all. An implementation that read it
        # instead of this bench's own port must report this bench down, so the
        # assertions below cannot pass under the defect.
        assert _http_code(inst.name, inst.first_port, SECOND_SITE) == "000"

        out, blocks = _axi_status_blocks(inst.name, "--bench", str(inst.second_index))
        second = blocks[SECOND_BENCH_PATH]
        assert second["web_port"] == str(inst.second_port), out
        assert second["web_site"] == SECOND_SITE, out
        assert second["web_http_code"] == "200", out
        assert second["overall"] == "running", out
        # No self-contradiction: a bench reporting RUNNING processes reports a
        # live web code beside them.
        assert second["supervisor_up"] == "true", out

        # The instance fold: a never-started bench beside a serving one is
        # `running`, not `online` and not `degraded`.
        whole_out, whole = _axi_status_blocks(inst.name)
        assert whole_out.splitlines()[0] == "overall: running", whole_out
        assert whole[FIRST_BENCH_PATH]["overall"] == "online", whole_out
        assert whole[SECOND_BENCH_PATH]["overall"] == "running", whole_out
        assert whole[SECOND_BENCH_PATH]["web_http_code"] == "200", whole_out

        # The human surface says the same thing, in its one stdout token.
        human = harness.run_cwcli("status", inst.name, "--bench", str(inst.second_index))
        assert human.returncode == 0, human.stdout + human.stderr
        assert harness.strip_ansi(human.stdout).strip() == "running", human.stdout + human.stderr
    finally:
        _serve_both(inst)


# --------------------------------------------------------------------------- #
# F4. a bench serving nothing never reports its neighbour's live code
# --------------------------------------------------------------------------- #
@v16_only
def test_f4_a_stopped_bench_never_borrows_its_neighbours_http_code(two_benches):
    """The converse misreport: the second bench is down while the first serves.

    The bench-blind probe reported the FIRST bench's live code for a bench whose
    web process was stopped - so ``web_http_code`` carried zero information about
    any bench past the first, and the "web is up but not actually serving" fault
    class it exists to catch was invisible there.
    """
    inst = two_benches
    _serve_both(inst)
    try:
        stop = harness.run_cwcli("stop", inst.name, "--bench", str(inst.second_index))
        assert stop.returncode == 0, stop.stdout + stop.stderr
        _wait_code(inst.name, inst.second_port, "000")

        # POSITIVE FIRST: the first bench is genuinely alive and answering for its
        # own site - so there IS a live code available to be wrongly borrowed. A
        # run where both benches were down would pass the negative below for the
        # wrong reason.
        assert _http_code(inst.name, inst.first_port, FIRST_SITE) == "200"
        borrowed_code = _http_code(inst.name, inst.first_port, SECOND_SITE)
        assert borrowed_code != "000"
        # And the second bench genuinely serves nothing.
        assert _http_code(inst.name, inst.second_port, SECOND_SITE) == "000"

        out, blocks = _axi_status_blocks(inst.name, "--bench", str(inst.second_index))
        second = blocks[SECOND_BENCH_PATH]
        assert second["web_port"] == str(inst.second_port), out
        assert second["web_http_code"] in ("000", "null"), out
        assert second["web_http_code"] != "200", out
        # The live-neighbour precondition above can hold even when the actual
        # discriminator would not. Assert the defect's exact signature so a
        # future variant cannot pass, and the test proves what it claims about
        # the value the removed bench-blind probe would have read.
        assert second["web_http_code"] != borrowed_code, out

        # Both benches in ONE document: the live code belongs to the bench that
        # earned it, and the dead one is reported dead.
        whole_out, whole = _axi_status_blocks(inst.name)
        assert whole[FIRST_BENCH_PATH]["web_http_code"] == "200", whole_out
        assert whole[SECOND_BENCH_PATH]["web_http_code"] != "200", whole_out
        assert whole[SECOND_BENCH_PATH]["web_http_code"] != borrowed_code, whole_out
        assert whole[FIRST_BENCH_PATH]["overall"] == "running", whole_out
        # A deliberate `stop --bench` clears the marker, so the stopped bench is
        # honestly `online` (never started), not `degraded` (started, died).
        assert whole[SECOND_BENCH_PATH]["overall"] == "online", whole_out
        assert whole_out.splitlines()[0] == "overall: running", whole_out
    finally:
        _serve_both(inst)


# --------------------------------------------------------------------------- #
# F5. start's web wait watches the bench it was told to start
# --------------------------------------------------------------------------- #
@v16_only
def test_f5_starting_a_non_first_bench_waits_on_its_own_port(two_benches, capsys):
    """``start --bench N`` for N>0 used to wait on ``:8000`` and warn falsely.

    With the first bench down, nothing answers its port at all, so a wait that
    watched it would spend its whole 60-second timeout and then tell the user a
    bench that had been serving the entire time had failed to start - and route
    them to ``cwcli status``, which (F3) agreed.
    """
    inst = two_benches
    _serve_both(inst)
    try:
        for index, port in (
            (inst.first_index, inst.first_port),
            (inst.second_index, inst.second_port),
        ):
            stop = harness.run_cwcli("stop", inst.name, "--bench", str(index))
            assert stop.returncode == 0, stop.stdout + stop.stderr
            _wait_code(inst.name, port, "000")

        # THE DISCRIMINATOR, asserted before the start: the first bench's port
        # answers nothing, so a wait pointed there cannot succeed.
        assert _http_code(inst.name, inst.first_port) == "000"

        started_at = time.monotonic()
        res = harness.run_cwcli("axi", "start", inst.name, "--bench", str(inst.second_index))
        elapsed = time.monotonic() - started_at
        assert res.returncode == 0, res.stdout + res.stderr

        # POSITIVE FIRST: the bench it started genuinely serves. A negative
        # assertion about false warnings also passes when nothing happened at
        # all, which is how the old empty bench skeleton hid this defect. The
        # first bench's port is STILL dead, so the wait cannot have succeeded
        # there; the only port that answered is the one this bench owns.
        assert _http_code(inst.name, inst.second_port, SECOND_SITE) == "200"
        assert _http_code(inst.name, inst.first_port) == "000"

        out = harness.strip_ansi(res.stdout)
        combined = out + harness.strip_ansi(res.stderr)
        # The honest signal: the web wait observed a serving port and said so.
        assert "web_ready: true" in out, out
        assert f"bench_path: {SECOND_BENCH_PATH}" in out, out
        # ... and no false alarm about a bench that started perfectly well.
        assert "web_not_ready" not in combined, combined
        assert "did not begin serving" not in combined, combined

        with capsys.disabled():
            print(
                f"\n[cwcli-latency] axi start --bench {inst.second_index} "
                f"(second bench, first bench down): {elapsed:.1f}s"
            )
    finally:
        _serve_both(inst)


# --------------------------------------------------------------------------- #
# 5.5 (part 1). instance-wide status latency on a real TWO-bench instance
# --------------------------------------------------------------------------- #
def _median_seconds(run, repeats: int = 5) -> float:
    samples = []
    for _ in range(repeats):
        started = time.monotonic()
        res = run()
        samples.append(time.monotonic() - started)
        assert res.returncode == 0, res.stdout + res.stderr
    samples.sort()
    return samples[len(samples) // 2]


@v16_only
def test_instance_wide_status_latency_on_two_serving_benches(two_benches, capsys):
    """``report-status-per-bench`` task 5.5, the two-bench half.

    The instance-wide form runs the full probe set once per bench, so the open
    question was whether the per-bench marginal cost justifies hoisting
    ``discover_stack``'s container-wide ``ps`` to one call per invocation. This
    measures it against two genuinely serving benches - a bench that is not
    serving skips ``supervisorctl`` and gets an instant connection refusal instead
    of a real HTTP response, so it would understate the cost being asked about.

    Reported, not pre-optimised: the ceiling asserted here is deliberately loose
    (it catches a catastrophic regression, not a few hundred milliseconds), and the
    numbers themselves are printed for the record.
    """
    inst = two_benches
    _serve_both(inst)

    one = _median_seconds(
        lambda: harness.run_cwcli("axi", "status", inst.name, "--bench", str(inst.first_index))
    )
    two = _median_seconds(lambda: harness.run_cwcli("axi", "status", inst.name))

    with capsys.disabled():
        print(
            f"\n[cwcli-latency] axi status, {harness.FRAPPE_BRANCH}: "
            f"1 bench (--bench) {one:.2f}s | 2 benches (instance-wide) {two:.2f}s | "
            f"marginal per bench {two - one:.2f}s"
        )

    assert two < 30, f"instance-wide status on 2 benches took {two:.1f}s"


# --------------------------------------------------------------------------- #
# `cwcli axi url`: the same bench-blind-probe defect class, on its own verb
# --------------------------------------------------------------------------- #
def _real_host_port(project: str, container_port: int) -> int:
    """The ACTUAL Docker-published host port, read via the CLI - independent of
    ``resolve_host_web_url``'s own docker-py read, so a passing test is not just
    comparing cwcli's answer to itself."""
    cid = harness.frappe_container_id(project)
    r = subprocess.run(
        ["docker", "port", cid, f"{container_port}/tcp"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return int(r.stdout.strip().splitlines()[0].rsplit(":", 1)[1])


def _axi_url_fields(project: str, *args: str) -> tuple[dict[str, str], str]:
    res = harness.run_cwcli("axi", "url", project, *args)
    assert res.returncode == 0, res.stdout + res.stderr
    out = harness.strip_ansi(res.stdout)
    fields: dict[str, str] = {}
    for line in out.splitlines():
        if not line or line.startswith(" "):
            continue
        if line.endswith(":"):
            break  # a block header ("warnings[N]:") - the flat fields precede it
        if ": " not in line:
            continue
        key, _, value = line.partition(": ")
        fields[key.strip()] = value.strip().strip('"')
    return fields, out


@v16_only
def test_axi_url_targets_each_benchs_own_port_never_a_neighbours(two_benches):
    """``cwcli axi url``'s whole reason to exist, proven the same way F3/F4 are:
    each bench's reported URL is that bench's OWN Docker-published port, and its
    probe answers for that bench's OWN site - never bench 0's when asked about
    bench 1, or vice versa. This is the exact defect class ``report-status-per-
    bench`` fixed for ``status``, reused here rather than re-derived."""
    inst = two_benches
    _serve_both(inst)

    first_host_port = _real_host_port(inst.name, inst.first_port)
    second_host_port = _real_host_port(inst.name, inst.second_port)
    # The fixture precondition this test leans on: two DIFFERENT container ports,
    # so two different host ports too (a shared port would make this vacuous).
    assert first_host_port != second_host_port

    first, first_out = _axi_url_fields(inst.name, "--bench", str(inst.first_index))
    assert first["bench_path"] == FIRST_BENCH_PATH, first_out
    assert first["site"] == FIRST_SITE, first_out
    assert first["url"] == f"http://{FIRST_SITE}:{first_host_port}", first_out
    assert first["reachable"] == "true", first_out
    assert first["http_code"] == "200", first_out

    second, second_out = _axi_url_fields(inst.name, "--bench", str(inst.second_index))
    assert second["bench_path"] == SECOND_BENCH_PATH, second_out
    assert second["site"] == SECOND_SITE, second_out
    assert second["url"] == f"http://{SECOND_SITE}:{second_host_port}", second_out
    assert second["reachable"] == "true", second_out
    assert second["http_code"] == "200", second_out

    # THE DISCRIMINATOR: neither bench's reported URL/port is the other's. A
    # bench-blind implementation (the removed ``status`` defect, reincarnated
    # here) would report the SAME port for both.
    assert first["url"] != second["url"], (first_out, second_out)


@v16_only
def test_axi_url_reports_a_stopped_benchs_own_url_as_unreachable(two_benches):
    """A bench whose dev processes are stopped still resolves ITS OWN url (the
    container and port config are untouched), but the fresh probe reports it
    honestly unreachable - and the sibling bench, still serving, is unaffected."""
    inst = two_benches
    _serve_both(inst)
    try:
        stop = harness.run_cwcli("stop", inst.name, "--bench", str(inst.second_index))
        assert stop.returncode == 0, stop.stdout + stop.stderr
        _wait_code(inst.name, inst.second_port, "000")

        second, second_out = _axi_url_fields(inst.name, "--bench", str(inst.second_index))
        assert second["bench_path"] == SECOND_BENCH_PATH, second_out
        assert second["reachable"] == "false", second_out
        assert second["http_code"] == "null", second_out
        # The URL itself is still resolved (the port config never went away) -
        # only the freshly-observed reachability changed.
        assert second["url"].endswith(f":{_real_host_port(inst.name, inst.second_port)}")

        # The sibling bench is a separate observation, unaffected by its neighbour.
        first, first_out = _axi_url_fields(inst.name, "--bench", str(inst.first_index))
        assert first["reachable"] == "true", first_out
        assert first["http_code"] == "200", first_out
    finally:
        _serve_both(inst)


@v16_only
def test_axi_url_with_no_bench_on_a_multibench_project_is_a_usage_error(two_benches):
    """No ``--bench`` on a genuinely multi-bench project is ambiguous - a usage
    error naming the flag, never a guess at which bench's URL was meant."""
    inst = two_benches
    _serve_both(inst)

    res = harness.run_cwcli("axi", "url", inst.name)
    assert res.returncode == 2, res.stdout + res.stderr
    out = harness.strip_ansi(res.stdout)
    assert "--bench" in out, out


# --------------------------------------------------------------------------- #
# 6. cwcli axi inspect / cwcli inspect --bench <index|label> (GitHub #226)
# --------------------------------------------------------------------------- #
@v16_only
def test_axi_inspect_bench_narrows_the_toon_document_to_one_bench(two_benches):
    """The agent surface's most-hit gap: ``axi inspect`` had no ``--bench`` at
    all, forcing a full-project inspect plus a TOON grep as the workaround on
    every multi-bench instance. ``--bench <index>`` must narrow the emitted
    document to exactly that bench, real Docker included."""
    inst = two_benches

    res = harness.run_cwcli("axi", "inspect", inst.name, "--bench", str(inst.second_index))
    assert res.returncode == 0, res.stdout + res.stderr
    out = harness.strip_ansi(res.stdout)
    assert "benches[1]:" in out, out
    blocks = _bench_blocks(out, key_field="path")
    assert set(blocks) == {SECOND_BENCH_PATH}, out


@v16_only
def test_axi_inspect_bench_by_label_narrows_too(two_benches):
    """The same selector by user label, not just numeric index."""
    inst = two_benches
    label = harness.run_cwcli("label", inst.name, str(inst.first_index), "primary")
    assert label.returncode == 0, label.stdout + label.stderr
    try:
        res = harness.run_cwcli("axi", "inspect", inst.name, "--bench", "primary")
        assert res.returncode == 0, res.stdout + res.stderr
        out = harness.strip_ansi(res.stdout)
        blocks = _bench_blocks(out, key_field="path")
        assert set(blocks) == {FIRST_BENCH_PATH}, out
    finally:
        harness.run_cwcli("label", inst.name, str(inst.first_index), "--clear")


@v16_only
def test_axi_inspect_unknown_bench_is_the_shared_not_found_error(two_benches):
    """An unknown ``--bench`` selector fails the same typed way every other
    bench-scoped ``axi`` verb does (exit 1, a ``bench.not_found``-style message),
    never a silent full-project fallback."""
    inst = two_benches

    res = harness.run_cwcli("axi", "inspect", inst.name, "--bench", "no-such-bench")
    assert res.returncode == 1, res.stdout + res.stderr
    out = harness.strip_ansi(res.stdout)
    assert "no-such-bench" in out, out


@v16_only
def test_axi_inspect_with_no_bench_still_reports_every_bench(two_benches):
    """The no-flag path is UNCHANGED: every cached bench, in one document."""
    inst = two_benches

    res = harness.run_cwcli("axi", "inspect", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    out = harness.strip_ansi(res.stdout)
    assert "benches[2]:" in out, out
    blocks = _bench_blocks(out, key_field="path")
    assert set(blocks) == {FIRST_BENCH_PATH, SECOND_BENCH_PATH}, out


@v16_only
def test_human_inspect_bench_narrows_json_output(two_benches):
    """The human CLI takes the same selector and narrows the same way."""
    inst = two_benches

    res = harness.run_cwcli(
        "inspect", inst.name, "--bench", str(inst.second_index), "--json", "--no-refresh"
    )
    assert res.returncode == 0, res.stdout + res.stderr
    doc = json.loads(res.stdout)
    assert [b["path"] for b in doc["bench_instances"]] == [SECOND_BENCH_PATH], res.stdout
