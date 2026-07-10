"""Fake Frappe container supporting multiple bench paths for multi-bench rm tests."""

import io
import shlex
import tarfile

from tests.test_rm_safety import BENCH, _tar_bytes


class FakeFrappeContainerMB:
    """A frappe container that serves multiple bench paths with independent
    sites, artifacts, and backup success per bench.

    ``per_bench`` maps ``bench_path -> dict`` with keys:
        ``sites`` (list[str]), ``artifacts`` (dict), ``backup_ok`` (bool or per-site dict),
        ``extra_entries`` (list[str]), ``ambiguous_entries`` (list[str]).

    Sites/extra/ambiguous entries that are identical across benches can also
    be set via the top-level kwargs (any per-bench value wins over top-level).
    """

    def __init__(
        self,
        per_bench: dict[str, dict],
        *,
        name="proj-frappe-1",
    ):
        self.per_bench = per_bench
        self.name = name
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.calls: list[str] = []
        self.get_archive_calls: list[str] = []
        self.stopped = False
        self.removed = False
        self._list_ok = True

    def _bench_data(self, bp):
        return self.per_bench.get(bp, {})

    def _sites(self, bp):
        d = self._bench_data(bp)
        return list(d.get("sites", []))

    def _extra(self, bp):
        return list(self._bench_data(bp).get("extra_entries", []))

    def _ambiguous(self, bp):
        return list(self._bench_data(bp).get("ambiguous_entries", []))

    def _backup_ok(self, bp):
        return self._bench_data(bp).get("backup_ok", True)

    def _backup_exit(self, bp, site):
        ok = self._backup_ok(bp)
        if isinstance(ok, dict):
            ok = ok.get(site, True)
        return 0 if ok else 1

    def _artifacts(self, bp):
        return self._bench_data(bp).get("artifacts", None)

    def _default_artifacts(self, bp):
        return {s: {"backup-%s-database.sql.gz" % s: b"DBDUMPBYTES",
                     "backup-%s-site_config_backup.json" % s: b"{}"} for s in self._sites(bp)}

    def exec_run(self, cmd, workdir=None):
        self.calls.append(cmd)
        for bp in self.per_bench:
            b = bp
            sites = self._sites(bp)
            extra = self._extra(bp)
            ambiguous = self._ambiguous(bp)

            if cmd == f"ls -1 {b}/sites":
                listing = ["apps.txt", "common_site_config.json", *extra, *ambiguous, *sites]
                return (0, "\n".join(listing).encode())

            if cmd.startswith("sh -c '") and "echo SITE" in cmd and "site_config.json" in cmd:
                entry = shlex.split(cmd)[-1].rsplit("/", 1)[-1]
                if f"{b}/sites/" in cmd:
                    if entry in sites:
                        return (0, b"SITE\n")
                    if entry in ambiguous:
                        return (0, b"AMBIGUOUS\n")
                    return (0, b"NOTASITE\n")

            if cmd.startswith("sh -c 'cd ") and "bench --site " in cmd and "backup" in cmd:
                site = cmd.split("bench --site ")[1].split(" ")[0]
                if f"cd {b}" in cmd:
                    return (self._backup_exit(bp, site), b"backup output")

            for site in sites:
                sbdir = f"{b}/sites/{site}/private/backups"
                if cmd == f"ls -1t {sbdir}":
                    arts = self._artifacts(bp)
                    if arts is None:
                        arts = self._default_artifacts(bp)
                    return (0, "\n".join(arts.get(site, {}).keys()).encode())

        return (1, b"")

    def get_archive(self, path):
        self.calls.append(f"get_archive {path}")
        self.get_archive_calls.append(path)
        for bp in self.per_bench:
            b = bp
            sites = self._sites(bp)
            arts = self._artifacts(bp)
            if arts is None:
                arts = self._default_artifacts(bp)
            for site in sites:
                prefix = f"{b}/sites/{site}/private/backups/"
                if path.startswith(prefix):
                    fname = path[len(prefix):]
                    data = arts.get(site, {}).get(fname)
                    if data is None:
                        raise FileNotFoundError(path)
                    raw = _tar_bytes(fname, data)

                    def _chunks(blob=raw):
                        for i in range(0, len(blob), 4):
                            yield blob[i:i+4]

                    return _chunks(), {"name": fname, "size": len(data)}
        raise FileNotFoundError(path)

    def stop(self):
        self.stopped = True

    def remove(self, v=False, force=False):
        self.removed = True

    def ran_bench_backup(self, bp) -> bool:
        prefix = f"cd {bp} && bench --site"
        return any(prefix in c for c in self.calls)