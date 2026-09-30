"""The batched bench read: every bench's facts in ONE container exec.

Every ``docker exec`` costs a fixed ~80 ms round trip whatever it does, and every
``bench --site X list-apps`` or ``bench --site X execute ...`` boots the bench CLI
and Frappe for one site (0.75-1.9 s). A full inspect used to pay both per fact and
per site. Here one exec runs one Python process per bench, sequentially: the
filesystem facts, the BUG-10 import probe, then ``import frappe`` ONCE and a loop
over the bench's sites with the same ``init``/``connect``/.../``destroy`` sequence
Frappe's own ``list-apps`` uses to walk several sites.

The per-site answers are exactly what the bench commands print:

- ``list_apps`` reproduces ``bench --site X list-apps`` (the text-format body of
  ``frappe.commands.site.list_apps``, identical in v13-v16): the ``Installed
  Applications`` rows through the same padded template, else the
  ``frappe.get_installed_apps()`` names.
- ``installed`` is ``frappe.get_installed_apps()``, the value
  ``bench --site X execute frappe.get_installed_apps`` prints.

The process only READS. It carries RAW facts; every interpretation (config JSON,
the marker, site verdicts, the import comparison, line splitting) stays on the
host in the shared parsers.

A question the process could not answer carries its cause instead: ``error`` on
the bench (no bench virtualenv, ``import frappe`` failed) or on the site
(``frappe.init``/``connect`` or the read failed). A cause is the failed step and
the exception's TYPE only, never its message, which can quote a site's config
(a database name or host). A bench whose process left no record at all is absent
from ``benches`` and its cause is in ``errors``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..utils import bench_labels, bench_sites
from . import resolvers

# Prefixes each bench's record line, so anything Frappe prints to stdout on the
# way can never be read as a record.
SENTINEL = "@@CWCLI-BENCH-READ@@"

# One Python process per bench. Runs under the bench venv's python (``venv=1``),
# else the system python3 (``venv=0``), which can still read the filesystem facts
# but must not answer the import probe or the Frappe questions for the bench.
# Kept to Python 3.6 syntax: v13 benches run old interpreters.
_READ_PY_BODY = """\
import json, os, subprocess, sys

opts = json.loads(sys.argv[1])
bench = sys.argv[2]
venv = sys.argv[3] == "1"
root = bench.rstrip("/")


def text(data, strict):
    try:
        return data.decode("utf-8", "strict" if strict else "replace")
    except UnicodeDecodeError:
        return None


def run(argv, strict=False):
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return text(proc.stdout, strict)


def read(path, strict=False):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except Exception:
        return None
    return text(data, strict)


def probe(apps):
    import contextlib, io
    buf = io.StringIO()
    saved = sys.argv
    sys.argv = ["-c", root] + apps
    try:
        with contextlib.redirect_stdout(buf):
            exec(PROBE, {"__name__": "__cwcli_probe__"})
    except Exception:
        return None
    finally:
        sys.argv = saved
    return buf.getvalue()


def failed(step, error):
    return step + " failed (" + type(error).__name__ + ")"


def list_apps(frappe):
    apps = frappe.get_single("Installed Applications").installed_applications
    if apps:
        name_len = max(len(app.app_name) for app in apps)
        ver_len = max(len(app.app_version) for app in apps)
        template = f"{{0:{name_len}}} {{1:{ver_len}}} {{2}}"
        return "\\n".join(
            template.format(app.app_name, app.app_version, app.git_branch) for app in apps
        )
    return "\\n".join(frappe.get_installed_apps())


rec = {"path": bench, "venv": venv, "sites": {}}
if opts["files"]:
    rec["apps"] = run(["ls", "-1", bench + "/apps"])
    names = []
    if rec["apps"] is not None:
        names = [a for a in rec["apps"].strip().split("\\n") if a]
    rec["imports"] = probe(list(dict.fromkeys(names))) if venv and names else None
    rec["common"] = read(bench + "/sites/common_site_config.json")
    rec["current"] = read(bench + "/sites/currentsite.txt", strict=True)
    rec["marker"] = read(root + "/" + MARKER_REL)

asked = opts["list_apps"] or opts["installed"]
if asked and not venv:
    rec["error"] = "no bench virtualenv python at " + root + "/env/bin/python"

sites = opts["sites"]
if sites is None:
    rec["listing"] = run(["sh", "-c", LIST_SITES_SH, "sh", bench], strict=True)
    sites = []
    for line in (rec["listing"] or "").split("\\n"):
        verdict, sep, name = line.partition("\\t")
        if line and (not sep or verdict != "NOTASITE"):
            sites.append(name if sep else line)

for site in sites:
    rec["sites"][site] = {}
    if opts["files"]:
        rec["sites"][site]["config"] = read(bench + "/sites/" + site + "/site_config.json")

if venv and sites and asked:
    try:
        os.chdir(bench + "/sites")
        import frappe
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        frappe = None
        rec["error"] = failed("import frappe", error)
    for site in sites if frappe is not None else []:
        found = rec["sites"][site]
        step = "frappe.init"
        try:
            frappe.init(site=site)
            step = "frappe.connect"
            frappe.connect()
            if opts["list_apps"]:
                step = "list-apps"
                found["list_apps"] = list_apps(frappe)
            if opts["installed"]:
                step = "frappe.get_installed_apps"
                found["installed"] = list(frappe.get_installed_apps())
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            found["error"] = failed(step, error)
        finally:
            try:
                frappe.destroy()
            except KeyboardInterrupt:
                raise
            except BaseException:
                pass

try:
    sys.stdout.flush()
except Exception:
    pass
sys.__stdout__.write("\\n" + SENTINEL + json.dumps(rec) + "\\n")
sys.__stdout__.flush()
"""
_READ_PY = (
    "".join(
        f"{name} = {value!r}\n"
        for name, value in {
            "SENTINEL": SENTINEL,
            "LIST_SITES_SH": bench_sites._LIST_SITES_SCRIPT,
            "MARKER_REL": bench_labels.MARKER_REL_PATH,
            "PROBE": resolvers._APP_IMPORT_PROBE,
        }.items()
    )
    + _READ_PY_BODY
)

# Prefixes the line the shell prints for a bench whose process exited non-zero:
# ``<FAILED><exit code><TAB><bench path>``.
FAILED = "@@CWCLI-BENCH-READ-FAILED@@"

# Runs :data:`_READ_PY` once per bench, sequentially, so one Frappe import is alive
# at a time. Bench paths ride as argv, never interpolated. The trailing ``exit 0``
# makes a non-zero exec code mean the exec itself failed.
_READ_SH = (
    'script=$1; opts=$2; shift 2; for b in "$@"; do '
    'py="${b%/}/env/bin/python"; venv=1; '
    'if [ ! -x "$py" ]; then py=/usr/bin/python3; venv=0; fi; '
    '"$py" -c "$script" "$opts" "$b" "$venv" 2>/dev/null || '
    f'printf "\\n{FAILED}%s\\t%s\\n" "$?" "$b"; '
    "done; exit 0"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteRead:
    """One site's facts. ``None`` means that read failed (or was not asked for).

    ``error`` is why an asked Frappe question has no answer: the site's own failed
    step, else its bench's.
    """

    name: str
    site_config: dict | None
    list_apps: list[str] | None
    installed: list[str] | None
    error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchRead:
    """One bench's facts, interpreted by the shared parsers.

    ``sites`` is None when the ``sites/`` directory could not be listed.
    """

    path: str
    available_apps: list[str]
    app_imports: dict[str, resolvers.AppImport]
    common_site_config: dict | None
    sites: list[SiteRead] | None
    current_site: str | None
    label: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchRead:
    """The exec's outcome. ``benches`` holds the benches that left a record;
    ``errors`` names why each other requested bench has none."""

    exit_code: int
    output: str
    benches: dict[str, BenchRead]
    errors: dict[str, str]


def output_lines(text: str) -> list[str]:
    """``ls -1`` / ``bench list-apps`` output split into its non-empty lines."""
    return [line for line in text.strip().split("\n") if line]


def parse_config(text: str | None) -> dict | None:
    """A ``cat``-ed JSON config: unreadable, empty or malformed is None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        config: dict = json.loads(text)
    except json.JSONDecodeError:
        return None
    return config


def read_benches(
    container,
    bench_paths: list[str],
    *,
    sites: list[str] | None = None,
    list_apps: bool = False,
    installed: bool = False,
    files: bool = False,
) -> BatchRead:
    """Read ``bench_paths`` in one exec.

    ``files`` reads the inspect facts (``apps/``, the import probe, the configs, the
    default-site pointer, the label marker). ``sites`` names the sites to read;
    ``None`` lists each bench's real sites with the shared ``list_sites`` rules.
    ``list_apps`` and ``installed`` ask Frappe the two per-site questions.

    Raises what ``exec_run`` raises (a lost Docker connection); a failed exec or an
    unparseable record leaves the bench out of ``benches``, with its cause in
    ``errors``.
    """
    opts = json.dumps(
        {"sites": sites, "list_apps": list_apps, "installed": installed, "files": files}
    )
    exit_code, output = container.exec_run(
        ["sh", "-c", _READ_SH, "sh", _READ_PY, opts, *bench_paths]
    )
    raw: bytes = output[0] if isinstance(output, tuple) else output
    text = raw.decode("utf-8", errors="replace")
    benches: dict[str, BenchRead] = {}
    exited: dict[str, str] = {}
    if exit_code == 0:
        for line in text.split("\n"):
            if line.startswith(FAILED):
                code, _tab, path = line[len(FAILED) :].partition("\t")
                exited[path] = f"its read process exited with code {code}"
            if not line.startswith(SENTINEL):
                continue
            try:
                record = json.loads(line[len(SENTINEL) :])
                bench = _bench_read(record, sites)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            if bench.path in bench_paths:
                benches[bench.path] = bench
    errors = {
        path: (
            f"the read exec exited with code {exit_code}"
            if exit_code != 0
            else exited.get(path, "its read process left no readable record")
        )
        for path in bench_paths
        if path not in benches
    }
    return BatchRead(exit_code=exit_code, output=text.strip(), benches=benches, errors=errors)


def _str_or_none(value) -> str | None:
    return value if isinstance(value, str) else None


def _str_list_or_none(value) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def _bench_read(record: dict, explicit_sites: list[str] | None) -> BenchRead:
    path = record["path"]
    if not isinstance(path, str):
        raise TypeError("bench path")
    apps_text = _str_or_none(record.get("apps"))
    available_apps = output_lines(apps_text) if apps_text is not None else []

    names = list(dict.fromkeys(available_apps))
    app_imports = (
        resolvers.app_imports_from_probe(path, names, _str_or_none(record.get("imports")))
        if names
        else {}
    )

    bench_error = _str_or_none(record.get("error"))
    current = _str_or_none(record.get("current"))
    marker = _str_or_none(record.get("marker"))

    found = record.get("sites") or {}
    site_names: list[str] | None
    if explicit_sites is not None:
        site_names = list(explicit_sites)
    else:
        listing = _str_or_none(record.get("listing"))
        site_names = (
            None
            if listing is None
            else [s for line in listing.split("\n") if (s := bench_sites.site_from_verdict(line))]
        )

    site_reads = None
    if site_names is not None:
        site_reads = []
        for name in site_names:
            facts = found.get(name) or {}
            list_apps_text = _str_or_none(facts.get("list_apps"))
            site_reads.append(
                SiteRead(
                    name=name,
                    site_config=parse_config(_str_or_none(facts.get("config"))),
                    list_apps=output_lines(list_apps_text) if list_apps_text is not None else None,
                    installed=_str_list_or_none(facts.get("installed")),
                    error=_str_or_none(facts.get("error")) or bench_error,
                )
            )

    return BenchRead(
        path=path,
        available_apps=available_apps,
        app_imports=app_imports,
        common_site_config=parse_config(_str_or_none(record.get("common"))),
        sites=site_reads,
        current_site=(current.strip() or None) if current is not None else None,
        label=bench_labels.label_from_marker_text(marker) if marker is not None else None,
    )
