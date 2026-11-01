"""``core.url`` - the address a site is opened at, and whether its bench answers right now.

Neither question had an agent-facing answer before this module. ``cwcli axi status``
reports a bench's ``web_http_code``, but that field is measured against the
CONTAINER-internal port and never states the address a browser would use. Getting
either answer meant dropping to a raw ``docker inspect``/``curl`` against the
container - exactly the escape hatch the ``axi`` surface exists to close.

This module adds NO new address resolution and NO new HTTP-probing mechanism: it is
orchestration over already-tested primitives, reused rather than re-derived (the
``set_maintenance``/``resolve_container_and_bench`` precedent - promote or call,
never re-implement):

- :func:`resolvers.resolve_site_address` for the address, built the way Frappe's
  own ``bench browse`` builds it (the site's ``host_name``, else the site name with
  the bench's port while ``webserver_port`` is set), with that port mapped to the
  HOST port Docker publishes - and, from the same read, the bench's container web
  port (``webserver_port``, else the port its Procfile ``web:`` line binds; see
  :func:`resolvers.resolve_web_ports`).
- :func:`supervision.web_http_code` for the probe, run INSIDE the container against
  the bench's container web port, with the site named as the ``Host`` header
  (Frappe is multi-tenant and routes by ``Host``; a host-less request is correctly
  answered 404 by a healthy bench). It is called directly here, never through
  ``core.status``, so the read is fresh on every invocation.

When the address carries the bench's port only because ``webserver_port`` is set on
a site whose name is not a localhost one, it is still returned unchanged - it is
what Frappe itself reports for that site - but flagged ``internal_port`` with a
``url.internal_port`` warning and the steps that remove the key, because behind a
reverse proxy that port is internal and the address fails. Fixing that is the
administrator's call, so cwcli warns rather than rewriting the address.

A port cwcli cannot resolve is reported as ``url: null`` / ``reachable: false``
with a warning - a gap in cwcli's knowledge is not a fault in the bench, and
guessing ``:8000`` measures a sibling bench's server. The probe result is never
smoothed into a health verdict: ``http_code`` carries whatever code curl saw.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import resolvers, supervision
from .envelope import Message, Result, Status


@dataclass(frozen=True, slots=True, kw_only=True)
class FixStep:
    """One step that removes ``webserver_port``: what it does, and the command."""

    step: str
    command: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UrlProbe:
    """The address for one site of one bench, plus a fresh HTTP observation.

    ``site`` is whichever site the probe named in its ``Host`` header (an explicit
    ``--site``, else :func:`resolvers.resolve_representative_site`'s pick) - or
    ``None`` when the bench has no site at all, in which case the probe is
    host-less and ``url`` falls back to naming ``localhost``.

    ``source`` is where the address came from: ``host_name`` (the site's own) or
    ``site`` (its name). ``internal_port``, ``webserver_port_in`` and ``fix`` are
    :class:`resolvers.SiteAddress`'s flag, the config files setting the key, and
    the steps that remove it (empty unless ``internal_port``).

    ``reachable`` is ``http_code not in (None, "000")`` - the same "up" definition
    ``core.status`` uses: a bound port answering ANY code is up, unreachable is not.
    """

    project: str
    bench_path: str
    site: str | None
    url: str | None
    source: str
    reachable: bool
    http_code: str | None
    internal_port: bool
    webserver_port_in: list[str]
    fix: list[FixStep]


def probe_url(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    site: str | None = None,
) -> Result[UrlProbe]:
    """Resolve a site's address and probe its bench fresh, once, right now.

    Shares the container+bench prologue used by single-bench verbs such as
    ``axi logs`` (:func:`resolvers.resolve_container_and_bench`): a stopped container is a
    ``confirm_start`` ``NEEDS_CHOICE`` (never auto-started here - no ``--yes`` on
    this verb, matching ``axi logs``), and an ambiguous multi-bench
    project with no ``--bench`` is a ``select_bench`` ``NEEDS_CHOICE``. Either way
    the frontend renders it as a usage error naming the flag to pass.
    """
    warnings: list[Message] = []

    resolved = resolvers.resolve_container_and_bench(project_name, bench, bench_path)
    if isinstance(resolved, Result):
        return resolved
    container, resolved_bench_path, resolve_warnings = resolved
    warnings.extend(resolve_warnings)

    resolved_site: str | None
    if site is not None:
        resolvers.validate_site_name(site)
        resolvers.require_bench_dir(container, resolved_bench_path)
        resolvers.require_site_dir(container, resolved_bench_path, site)
        resolved_site = site
    else:
        resolved_site = resolvers.resolve_representative_site(project_name, resolved_bench_path)
        if resolved_site is None:
            warnings.append(
                Message(
                    "url.no_site",
                    f"No site found for bench {resolved_bench_path}; probing without a "
                    "Host header.",
                )
            )

    address = resolvers.resolve_site_address(container, resolved_bench_path, site=resolved_site)
    web_port = address.web_port
    if web_port is None:
        warnings.append(
            Message(
                "url.unresolved",
                f"Could not determine the web port of bench {resolved_bench_path} from its "
                "sites/common_site_config.json (webserver_port) or its Procfile (the web "
                "line), so the bench was not probed. Check that both files are readable, "
                "then retry.",
            )
        )
    elif address.url is None:
        warnings.append(
            Message(
                "url.unresolved",
                f"Container port {web_port} for bench {resolved_bench_path} has no "
                "live host binding. If the bench is outside the instance's published "
                f"range, run 'cwcli axi scale {project_name}'; otherwise repair the "
                "Docker port binding, then retry.",
            )
        )

    fix: list[FixStep] = []
    if address.internal_port and resolved_site is not None:
        fix = _fix_steps(project_name, resolved_bench_path, resolved_site, address)
        warnings.append(
            Message(
                "url.internal_port",
                f"{address.url} is the address Frappe reports for {resolved_site}, but it "
                f"carries the bench's port because webserver_port is set in "
                f"{' and '.join(address.webserver_port_in)}. If {resolved_site} is served "
                "by its hostname through a reverse proxy, that port is internal and this "
                "address does not open. To build the address without the port: "
                + "; ".join(f"{s.step}: {s.command}" for s in fix),
                detail={"fix": [{"step": s.step, "command": s.command} for s in fix]},
            )
        )

    http_code: str | None = None
    if web_port is not None and address.url is not None:
        http_code = supervision.web_http_code(container, port=web_port, site=resolved_site)

    return Result(
        status=Status.OK,
        data=UrlProbe(
            project=project_name,
            bench_path=resolved_bench_path,
            site=resolved_site,
            url=address.url,
            source=address.source,
            reachable=http_code not in (None, "000"),
            http_code=http_code,
            internal_port=address.internal_port,
            webserver_port_in=address.webserver_port_in,
            fix=fix,
        ),
        warnings=warnings,
    )


def _fix_steps(
    project: str, bench_path: str, site: str, address: resolvers.SiteAddress
) -> list[FixStep]:
    """The commands that remove ``webserver_port`` and make the running bench use it.

    Both removal commands are Frappe's and bench's own, run through ``cwcli run``:
    bench's ``config remove-common-config`` edits the bench's common config, and
    ``set-config <key> None`` is Frappe's documented way to delete a key from one
    site's config. The bench's processes are then restarted - only that bench, so
    sibling benches keep serving - because Frappe caches the site config in memory.
    """
    flag = resolvers.bench_cli_flag(project, bench_path)
    selector = f" --bench {_bench_selector(project, bench_path)}"
    steps: list[FixStep] = []
    if "sites/common_site_config.json" in address.webserver_port_in:
        steps.append(
            FixStep(
                step="Remove webserver_port from the bench's common_site_config.json",
                command=f"cwcli run {project}{flag} config remove-common-config webserver_port",
            )
        )
    if f"sites/{site}/site_config.json" in address.webserver_port_in:
        steps.append(
            FixStep(
                step=f"Remove webserver_port from {site}'s site_config.json",
                command=f"cwcli run {project}{flag} --site {site} set-config webserver_port None",
            )
        )
    steps += [
        FixStep(
            step="Stop the bench's processes so none keeps the old setting",
            command=f"cwcli stop {project}{selector}",
        ),
        FixStep(step="Start them again", command=f"cwcli start {project}{selector}"),
        FixStep(
            step=(
                f"Optionally, if {site} is served over HTTPS, record its address so "
                "Frappe and cwcli use it"
            ),
            command=f"cwcli run {project}{flag} --site {site} set-config host_name https://{site}",
        ),
    ]
    return steps


def _bench_selector(project: str, bench_path: str) -> str:
    """This bench's ``--bench`` value: its label, else its index (0 when uncached).

    ``bench_cli_flag`` leaves the flag out when it is not needed to SELECT the bench
    (and may answer ``--path``, which ``stop``/``start`` do not take), but ``cwcli
    stop <project>`` without one stops the whole instance, so the stop and start
    steps always name the bench with ``--bench``.
    """
    for position, cached in enumerate(resolvers.cached_benches(project)):
        if cached.get("path") == bench_path:
            return str(cached.get("label") or cached.get("index", position))
    return "0"
