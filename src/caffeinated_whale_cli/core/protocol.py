"""The web UI API's protocol number, shared by the daemon and the hub link.

Bumped on any change a page built against an older daemon could misread, so a
page compares it with the number it was built for instead of parsing versions,
and a hub allows actions on a remote machine only when the two daemons agree.

* 2: the /api/v1 routes and the {data}/{errors} envelope (Phase 1).
* 3: durable jobs, job SSE frames, and detached lifecycle actions (Phase 2).
* 4: the settings endpoint and the guarded destructive job verbs (Phase 3).
* 5: the apps catalog (/api/v1/apps, its logo route, the `catalog` SSE event).
* 7: roles, directory sign-in, and /api/v1/integrations (the S3 target moved there).
* 8: community integrations (the community_* integration ops, a card's community block).
* 9: publishing sites (/api/v1/publications, /api/v1/publish/*, the publish job verbs).
* 10: remote hosts: /api/v1/hosts, the per-host forward, the host_* SSE events, and
  the downgrade-only delegation headers a hub's bridge sends.
* 11: the dependency plan (/api/v1/apps/plan) and the install, new-site, new-bench and
  update job arguments `with_deps`, `dep_sources` and `ignore_frappe_range`.

It lives in the UI-pure core (not ``commands/serve.py``) because ``core.hosts``
needs it to apply the version rule without importing the daemon module.
"""

PROTOCOL_VERSION = 11

# The bridge's own protocol: what ``GET /api/v1/bridge`` answers. Bumped when the
# bridge's request rewrite or its answer shape changes.
BRIDGE_PROTOCOL = 1
