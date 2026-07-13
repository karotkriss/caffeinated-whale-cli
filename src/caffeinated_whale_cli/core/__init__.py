"""cwcli's UI-pure logic core.

Modules here own their I/O (Docker, subprocess, filesystem, the peewee
registry) but carry NO presentation and NO interaction: they never import
``rich`` or ``questionary``, never call ``typer.Exit``, and never prompt. A core
function is driven entirely by explicit params - when it hits a decision it
cannot make it either returns a ``NEEDS_CHOICE`` :class:`~.envelope.Result` or
raises a typed :class:`~.errors.CwcliError`. This keeps the CLI, the ``cwcli
axi`` surface, and any future GUI as thin serializer/renderer frontends over one
implementation.

See ``openspec/changes/core-logic-foundation/design.md`` for the seven locked
decisions this package implements.
"""
