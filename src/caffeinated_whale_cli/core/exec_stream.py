"""``core.exec_stream`` - the exec-stream contract: one exec, as typed events.

The rework's locked decision 4 says streaming operations return typed event
iterators and no live Docker object leaks past the core boundary. This is that
contract, and it is a PER-EXEC primitive rather than a per-verb one: ``run`` is
the only consumer with one exec per resolve, while ``update`` performs 13 and
``init`` 11 downstream of a single resolve, with real logic between them (enable
maintenance mode, discover affected sites, disable it again in a ``finally``). A
verb-shaped ``core.run(...) -> Iterator[RunEvent]`` could not express that.

It owns the four things the four hand-rolled loops each got wrong in their own
way:

1. **The honest exit code.** ``exec_inspect`` returns ``{"ExitCode": None,
   "Running": True}`` while an exec is still going, so the key is PRESENT and
   ``.get("ExitCode", 1)``'s default never fires. ``None`` then flowed straight
   to success: ``run.py`` raised ``typer.Exit(code=None)`` (which exits **0**)
   and ``apps.py`` returned ``result.get("ExitCode", 1) or 0`` (which is **0**),
   so a bench command that never finished reported success and a destructive
   ``uninstall-app`` reported ``"ok": True``. This matters because
   ``CancellableStream.__next__`` swallows ``ProtocolError``/``OSError`` into
   ``StopIteration``, making a DROPPED CONNECTION indistinguishable from a clean
   end of stream. So the code is polled (as ``update`` alone did correctly) and
   :class:`ExecDone` types it ``int``, never ``int | None`` - the fail-open is
   unrepresentable. When it is genuinely unknowable this raises rather than
   guessing: reporting 0 claims a success that did not happen, and reporting 1
   claims a command failure that may not have happened.
2. **The stream tag.** ``exec_start`` is always called with ``demux=True`` and
   every chunk is tagged, because ``init`` routes container stderr to
   ``sys.stderr`` and a tagless event would silently lose that when it migrates;
   the ``cwcli axi`` surface likewise cannot keep stdout pure without knowing
   which stream a byte came from. There is deliberately no ``demux`` parameter:
   docker-py builds the same ``frames_iter(socket, tty)`` in both modes and
   differs only in the per-frame wrapper, so a consumer writing both tags to
   stdout in yield order reproduces the untagged byte stream exactly, carriage
   returns and progress-bar redraws included.
3. **The decode**, via :func:`~..utils.docker_utils.utf8_stream_decoder`, one
   decoder PER stream (see that function's docstring for the boundary-split root
   cause, and why the two must never be shared).
4. **Closing the stream.** docker-py states the rule in
   ``APIClient._read_from_socket``: "If stream=True, then a generator is returned
   instead and the caller is responsible for closing the response." Not one of
   the four consumers did.

The frontend decides whether to render; the core always streams. The consumers'
needs look like a conflict and are not one - one iterator, three consumption
modes: render each event (``run``, verbose ``update``, human ``apps``), drain and
discard (non-verbose ``update``, which runs under a spinner that streaming would
shred), or drain and join (``apps --json``, whose stdout must hold only the JSON
document).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass

from docker.errors import DockerException

from ..utils.docker_utils import utf8_stream_decoder
from .errors import CwcliError, ErrorKind

# How long to keep asking the daemon for an exit code it has not recorded yet.
#
# The stream closing and the daemon recording the code are not atomic, so a read
# immediately after the stream ends can legitimately land in that window - it is
# milliseconds wide. The bound exists for the OTHER case: a dropped connection
# looks exactly like a clean end of stream (see the module docstring), and there
# the exec may still be running for minutes. Unbounded polling (what `update` did)
# hangs forever on that; this raises instead. Generous enough not to trip on a
# loaded daemon, short enough not to read as a hang.
_EXIT_CODE_POLL_TIMEOUT = 10.0
_EXIT_CODE_POLL_INTERVAL = 0.1


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecChunk:
    """One decoded piece of an exec's output, tagged with the stream it came from."""

    stream: str  # "stdout" | "stderr"
    text: str  # never split mid-character; see utf8_stream_decoder


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecDone:
    """The terminal event: the exec's genuinely-known exit code.

    A terminal EVENT rather than the generator's ``return`` value, because a
    return value is reachable only via ``StopIteration.value``, which no consumer
    would remember to read - and a forgotten exit code is the failure this
    primitive exists to end.
    """

    exit_code: int  # NOT `int | None`: the fail-open, typed out of existence


ExecEvent = ExecChunk | ExecDone


def _close_stream(stream) -> None:
    """Release the exec socket. Never let closing mask the real outcome."""
    close = getattr(stream, "close", None)
    if close is None:  # a test fake, or an already-materialized iterable
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - a failed close must not replace the real result
        pass


def _poll_exit_code(api, exec_id: str) -> int:
    """Ask the daemon for the exit code until it knows one, or admit we cannot.

    Returns only a code the daemon actually reported. Never coerces an unknown
    code to a number - see the module docstring for why that is the whole point.
    """
    deadline = time.monotonic() + _EXIT_CODE_POLL_TIMEOUT
    while True:
        try:
            result = api.exec_inspect(exec_id)
        except DockerException as e:
            # Same failure the streaming loop is designed to make honest: the
            # daemon connection dropped mid-poll, so the outcome is genuinely
            # unknown rather than a clean end of stream.
            raise CwcliError(
                ErrorKind.DOCKER,
                "exec.stream_lost",
                f"Lost the connection while checking the command's exit code: {e}",
                hint="Check the container, then re-run. Nothing about its outcome is known.",
            ) from e
        exit_code = result.get("ExitCode")
        if exit_code is not None:
            return int(exit_code)
        if time.monotonic() >= deadline:
            break
        time.sleep(_EXIT_CODE_POLL_INTERVAL)

    if result.get("Running"):
        # The stream ended while the exec is still running: the connection dropped
        # (CancellableStream turned it into a silent StopIteration). The command
        # may well still be running in the container, so "it failed" would be a
        # confident wrong answer.
        raise CwcliError(
            ErrorKind.DOCKER,
            "exec.stream_lost",
            "Lost the connection to the command's output stream; it may still be running.",
            hint="Check the container, then re-run. Nothing about its outcome is known.",
        )
    raise CwcliError(
        ErrorKind.DOCKER,
        "exec.exit_code_unknown",
        "Docker reported the command finished but recorded no exit code.",
        hint="Check the container's state; the command's outcome is unknown.",
    )


def exec_stream(
    container,
    cmd,
    *,
    workdir: str | None = None,
    environment: dict | None = None,
) -> Iterator[ExecEvent]:
    """Run ``cmd`` in ``container``, yielding its output then its exit code.

    Takes a LIVE container, like every other core primitive that execs
    (``resolvers.resolve_container_state``, ``resolvers.require_bench_dir``).
    That is not a violation of "no live Docker objects past the core boundary":
    that rule governs what a ``core.<verb>`` RETURNS, and a returned plan is
    exactly why ``RunPlan`` carries an ID string instead. A caller that only has
    an id turns it back into a handle with ``core.docker.get_container``.

    ``environment`` is passed through to ``exec_create`` so a caller can keep
    secrets off the exec's ``Cmd`` record (``init``'s ``$CWCLI_*`` pattern).

    Yields :class:`ExecChunk` per decoded chunk, then exactly one
    :class:`ExecDone`. Raises :class:`CwcliError` (``DOCKER``) if the exec cannot
    be started or the exit code cannot be established.
    """
    try:
        api = container.client.api
        # `environment` is only sent when there is one: docker-py defaults it to
        # None, so passing it unconditionally would be the same call to the
        # daemon while gratuitously widening what every caller must accept.
        extra = {"environment": environment} if environment else {}
        exec_id = api.exec_create(container.id, cmd, workdir=workdir, **extra)["Id"]
        stream = api.exec_start(exec_id, stream=True, demux=True)
    except DockerException as e:
        raise CwcliError(
            ErrorKind.DOCKER,
            "exec.start_failed",
            f"Could not start the command in the container: {e}",
        ) from e

    # One decoder per stream: sharing one would feed stderr's bytes into stdout's
    # pending character and mangle both.
    decoders = {"stdout": utf8_stream_decoder(), "stderr": utf8_stream_decoder()}
    try:
        for stdout, stderr in stream:
            for tag, raw in (("stdout", stdout), ("stderr", stderr)):
                if not raw:
                    continue
                text = decoders[tag].decode(bytes(raw))
                if text:
                    yield ExecChunk(stream=tag, text=text)
        # Surface a trailing incomplete character instead of dropping it: a
        # truncated stream should be visible, not silently swallowed.
        for tag, decoder in decoders.items():
            tail = decoder.decode(b"", final=True)
            if tail:
                yield ExecChunk(stream=tag, text=tail)
    finally:
        # Runs on an early `break` by the consumer and on an exception mid-render
        # too, so the socket is released rather than leaked.
        _close_stream(stream)

    yield ExecDone(exit_code=_poll_exit_code(api, exec_id))
