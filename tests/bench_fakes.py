"""Shared test fakes for the bench-labeling / --bench / marker-file features.

Not collected by pytest (no ``test_`` prefix). Imported by the label/selector/
recovery test modules.
"""

import base64
import shlex


class MarkerFakeContainer:
    """A fake frappe container backed by an in-memory 'filesystem' dict.

    It interprets EXACTLY the ``exec_run`` commands the marker I/O in
    ``utils/bench_labels.py`` emits (all list-form):

      - ``["cat", path]``               -> read a marker file
      - ``["rm", "-f", path]``          -> remove a marker file
      - ``["sh", "-c", <write script>]`` -> the ``mkdir -p ... && printf %s <b64> |
        base64 -d > <marker>`` write; the base64 is really decoded, so this
        exercises the exact bytes the real container would receive.

    It also answers the shared bench-existence probe
    (``resolvers.present_bench_paths``): ``present_paths=None`` (the default) means
    every path asked about exists, a set means only those do, and
    ``present_probe_fails=True`` makes the probe itself fail, which is how a caller
    reaches the honest ``unverified`` state.

    It also answers a few string-form probes (``cat``/``test -d``) so it can stand
    in for a container in higher-level flows. ``status`` defaults to running.
    """

    def __init__(
        self,
        bench_path="/workspace/frappe-bench",
        fs=None,
        present_paths=None,
        present_probe_fails=False,
    ):
        self.bench_path = bench_path
        self.fs: dict[str, bytes] = dict(fs or {})
        self.calls: list = []
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"
        self.name = "fake-frappe-1"
        self.present_paths = present_paths
        self.present_probe_fails = present_probe_fails

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None, environment=None):
        self.calls.append(cmd)

        if isinstance(cmd, (list, tuple)):
            if cmd[:1] == ["cat"]:
                path = cmd[1]
                if path in self.fs:
                    return (0, self.fs[path])
                return (1, b"")
            if cmd[:2] == ["rm", "-f"]:
                self.fs.pop(cmd[2], None)
                return (0, b"")
            if cmd[:2] == ["sh", "-c"]:
                if 'for p in "$@"' in cmd[2]:
                    return self._run_presence_probe(list(cmd[4:]))
                return self._run_write_script(cmd[2])
            return (1, b"")

        # String-form fallback probes.
        if isinstance(cmd, str):
            if cmd.startswith("cat "):
                path = cmd[len("cat ") :].strip()
                if path in self.fs:
                    return (0, self.fs[path])
                return (1, b"")
            if "test -d" in cmd:
                return (0, b"")
        return (1, b"")

    def _run_presence_probe(self, paths):
        """Stand in for the one-exec bench-existence probe."""
        if self.present_probe_fails:
            return (1, b"")
        present = paths if self.present_paths is None else self.present_paths
        return (0, "\n".join(p for p in paths if p in present).encode())

    def _run_write_script(self, script: str):
        # Parse ``mkdir -p <dir> && printf %s <b64> | base64 -d > <marker>``.
        tokens = shlex.split(script)
        try:
            b64 = tokens[tokens.index("%s") + 1]
            marker = tokens[tokens.index(">") + 1]
        except (ValueError, IndexError):
            return (1, b"")
        try:
            content = base64.b64decode(b64)
        except Exception:
            return (1, b"")
        self.fs[marker] = content
        return (0, b"")
