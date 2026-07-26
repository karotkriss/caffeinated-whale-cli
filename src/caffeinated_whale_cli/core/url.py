"""``core.url`` - what host URL a bench actually serves on, and whether it answers right now.

Neither question had an agent-facing answer before this module. ``cwcli axi status``
reports a bench's ``web_http_code``, but that field is measured against the
CONTAINER-internal port and never states the HOST address a browser would use; the
only place that host address is computed at all is :func:`resolvers.resolve_host_web_url`,
consumed today by ``core.open``'s success banner and the human ``cwcli init`` banner,
neither of which is reachable from ``cwcli axi``. Getting either answer meant dropping
to a raw ``docker inspect``/``curl`` against the container - exactly the escape hatch
the ``axi`` surface exists to close.

This module adds NO new URL resolution and NO new HTTP-probing mechanism: it is
orchestration over two already-tested primitives, reused rather than re-derived
(the ``set_maintenance``/``resolve_container_and_bench`` precedent - promote or
call, never re-implement):

- :func:`resolvers.resolve_host_web_url` for the URL (the container port from the
  bench's own ``common_site_config.json``, mapped through the container's live
  published port bindings - the two hops a guessed ``:8000`` used to skip).
- :func:`supervision.web_http_code` for the probe, run against the SAME container
  port ``resolve_host_web_url`` just resolved, with the site named as the ``Host``
  header (Frappe is multi-tenant and routes by ``Host``; a host-less request is
  correctly answered 404 by a healthy bench). It is called directly here, never
  through ``core.status``, so the read is fresh on every invocation regardless of
  whether some OTHER caller is mid-``--watch`` (``probe_web=False``) or on the
  ``fused=True`` tier - this module's own call is neither of those, so it is
  unaffected by and does not affect either.

An unresolvable port (unreadable/unparseable ``common_site_config.json``, or a
container port with no published host binding) is reported as ``url: null`` /
``reachable: false`` with a warning - the ``status.web_port_unknown`` precedent: a
gap in cwcli's knowledge is not a fault in the bench, and guessing ``:8000`` IS the
defect this whole path exists to avoid. The probe result is never smoothed into a
health verdict: ``http_code`` carries whatever code curl saw (a 404/500 is reported
verbatim, exactly as ``status`` already treats "any code is serving").
"""

from __future__ import annotations

from dataclasses import dataclass

from . import resolvers, supervision
from .envelope import Message, Result, Status


@dataclass(frozen=True, slots=True, kw_only=True)
class UrlProbe:
    """The resolved host URL for one bench, plus a fresh HTTP observation.

    ``site`` is whichever site the probe named in its ``Host`` header (an explicit
    ``--site``, else :func:`resolvers.resolve_representative_site`'s pick) - or
    ``None`` when the bench has no site at all, in which case the probe is
    host-less and ``url`` falls back to naming ``localhost``.

    ``reachable`` is ``http_code not in (None, "000")`` - the same "up" definition
    ``core.status`` uses: a bound port answering ANY code is up, unreachable is not.
    """

    project: str
    bench_path: str
    site: str | None
    url: str | None
    reachable: bool
    http_code: str | None


def probe_url(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    site: str | None = None,
) -> Result[UrlProbe]:
    """Resolve a bench's host URL and probe it fresh, once, right now.

    Shares the container+bench prologue every bench-scoped verb uses
    (:func:`resolvers.resolve_container_and_bench`): a stopped container is a
    ``confirm_start`` ``NEEDS_CHOICE`` (never auto-started here - no ``--yes`` on
    this verb, matching ``axi logs``/``axi status``), and an ambiguous multi-bench
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

    http_code: str | None = None
    ports = resolvers.resolve_assigned_ports(
        container, [resolved_bench_path], fill_defaults=False
    )
    assigned = ports.get(resolved_bench_path)
    url = (
        resolvers.resolve_host_web_url(
            container,
            resolved_bench_path,
            site=resolved_site,
            assigned_ports=assigned,
        )
        if assigned is not None
        else None
    )
    if url is None or assigned is None:
        warnings.append(
            Message(
                "url.unresolved",
                f"Could not resolve the host URL for bench {resolved_bench_path}. Run "
                f"'cwcli inspect {project_name}' to refresh its port config.",
            )
        )
    else:
        http_code = supervision.web_http_code(container, port=assigned[0], site=resolved_site)

    return Result(
        status=Status.OK,
        data=UrlProbe(
            project=project_name,
            bench_path=resolved_bench_path,
            site=resolved_site,
            url=url,
            reachable=http_code not in (None, "000"),
            http_code=http_code,
        ),
        warnings=warnings,
    )
