"""``core.fleet`` - the in-memory fleet model behind ``cwcli serve``.

A long-lived, UI-pure model of every cwcli instance on this Docker daemon, kept
current by two sources with very different costs:

* **INSTANT** - Docker's own event stream (:func:`event_loop`). A blocking
  generator in one thread, no polling, measured 0.4-1.4ms from daemon emit to
  receipt. It answers *container up/down* and NOTHING else: a ``start`` event
  fires seconds before the bench is serving, so a lifecycle event moves an
  instance to :data:`UNKNOWN` health, never to a healthy token.
* **FAST** - a poll of every RUNNING instance (:func:`probe_loop`) through
  ``core.status(..., fused=True)``, the one-exec read. It answers per-process
  health, which Docker cannot see at all: restarting a single supervisord program
  produces no Docker event whatsoever.

The **LAZY** tier is not here - it is ``core.inspect`` called on demand by the
detail endpoint, which already carries its own ``served_from`` /
``installed_apps_verified`` freshness labels.

Three properties are load-bearing and each has a test:

1. :func:`health_key` diffs **stable fields only**. A single volatile field
   (a probe duration, an uptime, a cpu percentage, a timestamp) inside that
   digest turns the quiet FAST tier back into a firehose - measured in the
   design spike as 8 deltas in 8 cycles of an instance that never changed.
2. **Event-stream loss re-bootstraps the whole model** (:func:`event_loop`),
   never replays with ``since=``. Docker keeps a fixed 256-event ring, and this
   daemon's own probe exec traffic flushes it in ~51 seconds, so a gap replay
   silently returns nothing. The event stream is a change NOTIFICATION, never a
   durable log.
3. **Unknown is a state, not a default.** A running instance nothing has probed
   yet is :data:`UNKNOWN`, a failed probe is :data:`UNKNOWN` plus a
   ``probe_error``, and an unreadable web port stays ``web_port: null`` /
   ``web_http_code: null`` with ``web_probed`` telling a reader whether the null
   means "unreachable" or "never asked". None of these may render as 0, a dash,
   or a healthy default.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, replace

import docker
from docker.errors import DockerException

from . import status as core_status
from .errors import CwcliError, ErrorKind
from .list import list_instances
from .status import BenchStatus

# A fifth token the one-shot ``core.status`` fold has no use for and deliberately
# does not define: a CLI verb always has an answer by the time it prints, while a
# streaming model has instances it has not reached yet. It means "no probe has
# answered for this instance", never "probably fine".
UNKNOWN = "unknown"

# How long after a container lifecycle event this instance keeps its web HTTP
# check on. The web probe writes one line into the developer's own bench access
# log per cycle, so it is OFF by default and enabled only (a) in this window and
# (b) while a UI client has the instance open (see :meth:`Fleet.set_focus`).
WEB_PROBE_WINDOW_S = 30.0

# The lifecycle actions worth reacting to. Deliberately NOT ``exec_*``: this
# daemon's own probes emit three exec events per cycle and would wake themselves.
EVENT_ACTIONS = ["start", "stop", "die", "kill", "restart", "destroy"]

_RECONNECT_DELAY_S = 2.0


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceState:
    """One instance as the daemon currently understands it (JSON-serializable).

    ``overall`` is ``core.status``'s token (``running``/``degraded``/``online``/
    ``offline``) once a probe has answered, else :data:`UNKNOWN`.

    ``web_probed`` disambiguates every ``web_http_code: null`` underneath it:
    False means the web check was not asked for on this cycle, True means it was
    asked and got no answer. Without it a reader cannot tell "not measured" from
    "measured, dead", which is exactly the honest-unknown collapse this model
    refuses.
    """

    project: str
    docker_status: str
    container_running: bool
    ports: list[str]
    overall: str
    web_probed: bool
    benches: list[BenchStatus]
    probe_error: str | None = None
    probed_at: float | None = None
    probe_ms: float | None = None


def health_key(state: InstanceState) -> tuple:
    """The STABLE-fields-only digest the FAST tier decides "did this change" on.

    Every field here is one a human would call a state change. Every VOLATILE
    field is excluded by construction and by test
    (``tests/test_core_fleet.py::TestHealthKey``): ``probe_ms``, ``probed_at``,
    and each process's ``uptime_s``, ``cpu_pct`` and ``rss_kb`` all differ on
    every single cycle of a perfectly steady instance, so including any one of
    them publishes a delta per tick forever.

    ``pid`` IS included and is not volatile: a stable process keeps its pid, and
    a changed one means the process died and was restarted between two probes -
    a genuine change the supervisord ``state`` alone can miss when both reads
    land on ``RUNNING``.

    A tuple, not a hash: it compares exactly, never collides, and is readable in
    a debugger. It is not persisted, so it needs no stability across versions.
    """
    return (
        state.docker_status,
        state.container_running,
        tuple(state.ports),
        state.overall,
        state.web_probed,
        state.probe_error,
        tuple(
            (
                b.index,
                b.bench_path,
                b.label,
                b.overall,
                b.supervisor_up,
                b.web_port,
                b.web_port_verified,
                b.web_site,
                b.web_http_code,
                b.not_cwcli_supervised,
                tuple((p.label, p.up, p.pid, p.state) for p in b.processes),
            )
            for b in state.benches
        ),
    )


def as_json(state: InstanceState) -> dict:
    """One instance as plain JSON-able data (every DTO underneath is a dataclass)."""
    return asdict(state)


class Fleet:
    """The shared model. Every mutation diffs :func:`health_key` and publishes.

    ``publish(tier, project, state, cause)`` is called outside the model lock but
    inside the mutation lock, so callbacks may read the model while concurrent
    mutations and their publications remain ordered. ``state=None`` means the
    instance is gone and ``cause`` names the Docker action/service behind an
    INSTANT delta (None otherwise).
    """

    def __init__(self, *, publish=None, web_probe_window_s: float = WEB_PROBE_WINDOW_S) -> None:
        self._lock = threading.Lock()
        self._mutation_lock = threading.Lock()
        self._instances: dict[str, InstanceState] = {}
        self._keys: dict[str, tuple] = {}
        self._focus: set[str] = set()
        self._web_until: dict[str, float] = {}
        self._window = web_probe_window_s
        self._publish = publish or (lambda tier, project, state, cause: None)

    def set_publish(self, publish) -> None:
        """Bind the delta sink after construction (the fan-out needs the fleet first)."""
        with self._mutation_lock:
            self._publish = publish

    # ---------------------------------------------------------------- reads

    def snapshot(self) -> list[dict]:
        """The whole model, JSON-able, sorted by project name."""
        with self._lock:
            states = sorted(self._instances.values(), key=lambda s: s.project)
        return [as_json(s) for s in states]

    def get(self, project: str) -> InstanceState | None:
        with self._lock:
            return self._instances.get(project)

    def running_projects(self) -> list[str]:
        with self._lock:
            return sorted(p for p, s in self._instances.items() if s.container_running)

    # ---------------------------------------------------- web-probe cadence

    def set_focus(self, projects: set[str]) -> None:
        """The instances a UI client currently has open (decision B).

        Process-level health polls every instance on the normal cadence; the web
        HTTP check is the expensive-in-trace one (one access-log line per cycle
        in a log a developer reads), so it runs only for instances a client is
        actually looking at, plus the window after a lifecycle event.
        """
        with self._lock:
            self._focus = set(projects)

    def should_probe_web(self, project: str) -> bool:
        with self._lock:
            return project in self._focus or time.monotonic() < self._web_until.get(project, 0.0)

    def note_lifecycle(self, project: str) -> None:
        """Open the post-event web-probe window for one instance."""
        with self._lock:
            self._web_until[project] = time.monotonic() + self._window

    # -------------------------------------------------------------- writes

    def bootstrap(self, *, cause: dict | None = None) -> None:
        """Rebuild the instance set from ``core.list_instances`` and publish deltas.

        This is the ONLY way instances enter or leave the model, so startup, a
        lifecycle event, and an event-stream reconnect all take one code path -
        the reconnect case being the mandatory one (see the module docstring).
        Health already probed for a surviving instance is CARRIED FORWARD, so a
        re-bootstrap of an unchanged fleet publishes nothing at all.

        Raises ``CwcliError(DOCKER)`` when the daemon is unreachable; the callers
        that run in a loop catch it and retry.
        """
        rows = list_instances().data or []
        deltas: list[tuple[str, InstanceState | None]] = []

        with self._mutation_lock:
            with self._lock:
                seen = set()
                for dto in rows:
                    seen.add(dto.project_name)
                    new = self._reconcile(dto.project_name, dto.status, list(dto.ports))
                    if self._store(new):
                        deltas.append((new.project, new))
                for gone in sorted(set(self._instances) - seen):
                    del self._instances[gone]
                    self._keys.pop(gone, None)
                    self._web_until.pop(gone, None)
                    deltas.append((gone, None))

            # Always the INSTANT tier: a bootstrap only ever reports container-lifecycle
            # facts, whether triggered at startup, by an event, or by a reconnect.
            for project, state in deltas:
                self._publish("instant", project, state, cause)

    def probe(self, project: str) -> None:
        """The FAST tier for one instance: one fused read, published only if changed."""
        state = self.get(project)
        if state is None or not state.container_running:
            return
        probe_web = self.should_probe_web(project)

        started = time.monotonic()
        try:
            report = core_status.status(project, probe_web=probe_web, fused=True).data
        except CwcliError as e:
            if e.kind is ErrorKind.NOT_FOUND:
                # The project is gone. The health tier cannot express that (it
                # describes an instance that exists), and Docker's own listing is
                # the authority, so hand it to the one code path that owns the
                # instance set rather than leaving a phantom row behind.
                self.bootstrap()
                return
            # An honest unknown, not a health verdict: the probe could not find
            # out. Reporting the last known good token here would be the
            # crying-wolf defect inverted - a stale green pill over a dead read.
            new = replace(
                state,
                overall=UNKNOWN,
                web_probed=probe_web,
                probe_error=f"{e.code}: {e.message}",
                probed_at=time.time(),
                probe_ms=(time.monotonic() - started) * 1000,
            )
        else:
            assert report is not None
            if not report.container_running:
                # The probe outran the stop event (measured: 54ms ahead of it), or
                # the event was lost. Adopting the report's ``offline`` here while
                # the model still held the container facts published a row saying
                # ``container_running: true, overall: offline`` - two fields
                # contradicting each other in the same document. Re-bootstrap
                # instead: one Docker read, one consistent delta, and the event
                # that follows is then correctly suppressed as no change.
                self.bootstrap()
                return
            new = replace(
                state,
                overall=report.overall,
                benches=report.benches,
                web_probed=probe_web,
                probe_error=None,
                probed_at=time.time(),
                probe_ms=(time.monotonic() - started) * 1000,
            )

        with self._mutation_lock:
            changed = False
            with self._lock:
                # Re-read under the lock: a lifecycle event may have landed mid-probe,
                # and its container state is fresher than what this probe started with.
                current = self._instances.get(project)
                if current is not None:
                    new = replace(
                        new,
                        docker_status=current.docker_status,
                        container_running=current.container_running,
                        ports=current.ports,
                    )
                    changed = self._store(new)
            if changed:
                self._publish("fast", project, new, None)

    def apply_event(self, event: dict) -> None:
        """Fold one Docker lifecycle event into the model (the INSTANT tier).

        The event is used as a NOTIFICATION only - the model is then rebuilt from
        Docker's own container list rather than inferred from the event's fields.
        One extra Docker call per lifecycle event buys immunity to a whole class
        of inference bug (which service maps to instance liveness, what a
        ``kill`` means for a container that restarts), and lifecycle events are
        rare by definition.
        """
        attrs = (event.get("Actor") or {}).get("Attributes") or {}
        project = attrs.get("com.docker.compose.project")
        if not project:
            return
        self.note_lifecycle(project)
        self.bootstrap(
            cause={
                "project": project,
                "action": event.get("Action"),
                "service": attrs.get("com.docker.compose.service"),
            }
        )

    # ------------------------------------------------------------- internals

    def _reconcile(self, project: str, docker_status: str, ports: list[str]) -> InstanceState:
        """The state an instance should hold given fresh container facts (lock held)."""
        running = docker_status == "running"
        prev = self._instances.get(project)
        if prev is None:
            return InstanceState(
                project=project,
                docker_status=docker_status,
                container_running=running,
                ports=ports,
                overall=UNKNOWN if running else core_status.OFFLINE,
                web_probed=False,
                benches=[],
            )
        new = replace(prev, docker_status=docker_status, container_running=running, ports=ports)
        if not running:
            # Stopped is a definitive health answer, and the bench rows describe
            # processes that are provably gone - keeping them would be a claim
            # backed by nothing (``core.status`` empties them for the same reason).
            # The probe timings go with them: they describe a health read, and a
            # stopped instance has none, so carrying the last one forward would
            # date-stamp an answer no probe produced.
            return replace(
                new,
                overall=core_status.OFFLINE,
                benches=[],
                web_probed=False,
                probe_error=None,
                probed_at=None,
                probe_ms=None,
            )
        if not prev.container_running:
            # Just came up. A container being up is NOT a healthy bench (measured
            # 6.6s apart in the design spike), so health goes UNKNOWN and waits
            # for the FAST tier rather than inheriting a token from before the stop.
            return replace(new, overall=UNKNOWN, benches=[], probe_error=None)
        return new

    def _store(self, state: InstanceState) -> bool:
        """Write one instance, returning whether its stable digest changed (lock held)."""
        key = health_key(state)
        changed = self._keys.get(state.project) != key
        self._instances[state.project] = state
        self._keys[state.project] = key
        return changed


# ------------------------------------------------------------------- threads


def event_loop(fleet: Fleet, stop: threading.Event, *, on_error=None) -> None:
    """Subscribe to Docker's event stream, re-bootstrapping on every (re)connect.

    Server-side filtering delivers exactly the lifecycle events and none of the
    hundreds of ``exec_*`` events the FAST tier generates. That filtering is
    reliable on the LIVE stream only - the same ``event`` filter silently returns
    an EMPTY set for any ``since=``/``until=`` historical query - which is one of
    two reasons this never replays history. The other is decisive: Docker's event
    buffer is a fixed 256-entry ring, and this daemon's own exec traffic flushes
    it in about 51 seconds, so any gap worth replaying is already gone. Reconnect
    means re-bootstrap, always.
    """
    while not stop.is_set():
        try:
            client = docker.from_env()
            stream = client.events(
                decode=True,
                filters={
                    "type": "container",
                    "label": "com.docker.compose.project",
                    "event": EVENT_ACTIONS,
                },
            )
            fleet.bootstrap()
            for event in stream:
                if stop.is_set():
                    return
                fleet.apply_event(event)
        except (DockerException, CwcliError, OSError) as e:
            if on_error is not None:
                on_error(e)
        stop.wait(_RECONNECT_DELAY_S)


def probe_loop(fleet: Fleet, stop: threading.Event, interval: float, *, on_error=None) -> None:
    """Run the FAST tier over every RUNNING instance, every ``interval`` seconds.

    Serial on purpose. The fused read measures ~250ms per instance, so six
    running instances fit a 2.5s tick with room to spare.
    ponytail: serial probing, move to a small thread pool if a fleet ever
    outgrows the interval (the symptom is ticks running late, not wrong data).
    """
    while not stop.is_set():
        for project in fleet.running_projects():
            if stop.is_set():
                return
            try:
                fleet.probe(project)
            except (DockerException, CwcliError, OSError) as e:
                if on_error is not None:
                    on_error(e)
        stop.wait(interval)
