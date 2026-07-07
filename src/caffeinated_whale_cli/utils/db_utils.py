import datetime
import json
import os
import sys
from pathlib import Path

from peewee import CharField, DateTimeField, ForeignKeyField, Model, SqliteDatabase, TextField

APP_NAME = ".cwcli"
CACHE_DIR = Path.home() / APP_NAME / "cache"
DB_PATH = CACHE_DIR / "cwc-cache.db"

# SECURITY: config_json rows are whitelist-filtered by _redact_config_for_cache
# before write, so the cache never stores DB credentials, encryption keys, or
# redis URLs. Restrictive filesystem permissions (0700 dir / 0600 file) remain
# as defense-in-depth. See "Cache never stores secrets" in AGENTS.md.

# Create cache directory with restricted permissions (0700 = owner-only access)
# This prevents other users on the system from reading cached credentials
CACHE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)

# Ensure existing directory has correct permissions
if CACHE_DIR.exists():
    try:
        CACHE_DIR.chmod(0o700)
    except (OSError, PermissionError):
        # On Windows or restricted filesystems, chmod may fail
        # Still proceed but permissions may not be as strict
        pass

db = SqliteDatabase(DB_PATH)


class BaseModel(Model):
    class Meta:
        database = db


class Project(BaseModel):
    name = CharField(unique=True)
    last_updated = DateTimeField()


class Bench(BaseModel):
    project = ForeignKeyField(Project, backref="benches")
    path = CharField()
    # Optional user-assigned label. NULL/empty means the bench has no user label
    # and is addressed only by its numeric index (its position in stable, sorted
    # discovery order). See utils/bench_labels.py for the label model. This column
    # was added after the initial schema, so initialize_database() migrates old
    # caches in place (see _migrate_bench_label_column).
    label = CharField(null=True)
    # Default site recorded in this bench's sites/currentsite.txt (written by
    # `bench use`). A bench records its default site in EITHER
    # common_site_config.json's `default_site` OR currentsite.txt; persisting the
    # latter lets get_default_site() resolve the default even when common config
    # has no `default_site` key. NULL when currentsite.txt is absent/unreadable.
    # Added after the initial schema (see _migrate_bench_current_site_column).
    current_site = CharField(null=True)


class Site(BaseModel):
    bench = ForeignKeyField(Bench, backref="sites")
    name = CharField()
    installed_apps = TextField()


class AvailableApp(BaseModel):
    bench = ForeignKeyField(Bench, backref="available_apps")
    name = CharField()

    class Meta:
        table_name = "available_apps"


class InstalledAppDetail(BaseModel):
    """
    Stores installed app details (name, version, branch) for each site.
    """

    # backref kept distinct so it does not shadow Site.installed_apps column
    site = ForeignKeyField(Site, backref="installed_app_details")
    name = CharField()
    version = CharField()
    branch = CharField()

    class Meta:
        table_name = "installed_apps"


class CommonSiteConfig(BaseModel):
    """
    Stores common_site_config.json data for each bench.
    This config applies to all sites within a bench.

    SECURITY WARNING: this table must never hold sensitive data. Sensitive fields
    (passwords, keys, redis URLs) are stripped by ``_redact_config_for_cache``
    before write; only the ``_COMMON_CONFIG_CACHE_KEYS`` whitelist (e.g.
    ``default_site``, ports, ``developer_mode``) is persisted. Nothing reads
    secrets back from the cache - every credential consumer reads live from the
    container or from CLI flags/prompts. Old caches are cleaned in place by
    ``_scrub_cached_config_secrets`` on init.
    """

    bench = ForeignKeyField(Bench, backref="common_config", unique=True)
    # Whitelisted JSON as text (secrets stripped by _redact_config_for_cache before write)
    config_json = TextField()

    class Meta:
        table_name = "common_site_config"


class SiteConfig(BaseModel):
    """
    Stores site_config.json data for each individual site.

    SECURITY WARNING: this table must never hold sensitive data. The site's
    database credentials (``db_password``) and ``encryption_key`` are stripped by
    ``_redact_config_for_cache`` before caching; only the
    ``_SITE_CONFIG_CACHE_KEYS`` whitelist (e.g. ``db_name``, ``db_type``,
    ``developer_mode``) is persisted. Nothing reads secrets back from the cache.
    Old caches are cleaned in place by ``_scrub_cached_config_secrets`` on init.
    """

    site = ForeignKeyField(Site, backref="site_config", unique=True)
    # Whitelisted JSON as text (secrets stripped by _redact_config_for_cache before write)
    config_json = TextField()

    class Meta:
        table_name = "site_config"


# Whitelists of the ONLY keys persisted to the cache DB from each Frappe config.
# Everything else (db_password, encryption_key, admin/root passwords, redis_* URLs,
# *_secret, *_token, ...) is dropped at write time by _redact_config_for_cache.
# Whitelist, not blacklist: a future unknown Frappe secret key fails closed.
# Any NEW cached config field must be added here deliberately.
_COMMON_CONFIG_CACHE_KEYS = frozenset(
    {
        "default_site",  # read by get_default_site (restore/backup/unlock)
        "developer_mode",
        "webserver_port",
        "socketio_port",
        "file_watcher_port",
        "restart_supervisor_on_update",
        "frappe_user",
        "serve_default_site",
        "rebase_on_pull",
        "shallow_clone",
        "background_workers",
        "live_reload",
    }
)

_SITE_CONFIG_CACHE_KEYS = frozenset(
    {
        "db_name",  # identifies the site's database, not a credential
        "db_type",
        "developer_mode",
        "maintenance_mode",
        "pause_scheduler",
    }
)


def _redact_config_for_cache(config: dict, allowed_keys: frozenset[str]) -> dict:
    """Whitelist-filter a Frappe config dict before caching.

    Secrets (db_password, encryption_key, admin passwords, redis URLs, ...) must
    never be persisted to the cache DB. Nothing reads them back from the cache;
    every credential consumer reads live from the container or from CLI flags.
    Whitelist (not blacklist) so a future Frappe secret key fails closed - an
    unknown key is simply dropped rather than silently leaking.

    A non-dict input is returned unchanged so the immediately-following
    ``_validate_config_json`` still raises ``TypeError`` on it (preserving the
    existing skip-and-warn path), instead of crashing here on ``.items()``.
    """
    if not isinstance(config, dict):
        return config
    return {k: v for k, v in config.items() if k in allowed_keys}


def _validate_config_json(config_data: dict, config_type: str = "config") -> None:
    """
    Validate configuration JSON structure and size.

    Args:
        config_data: Dictionary containing configuration data (empty dicts are allowed)
        config_type: Type of config for error messages (e.g., "site_config", "common_site_config")

    Raises:
        ValueError: If config data is too large
        TypeError: If config data is not a dictionary

    Note:
        Empty dicts {} are considered valid - they represent config files with no custom settings.
    """
    if not isinstance(config_data, dict):
        raise TypeError(f"{config_type} must be a dictionary, got {type(config_data).__name__}")

    # Check size of serialized JSON (prevent extremely large configs)
    # Limit to 1MB of JSON data (reasonable for config files)
    serialized = json.dumps(config_data)
    if len(serialized) > 1_000_000:  # 1MB
        raise ValueError(
            f"{config_type} is too large ({len(serialized)} bytes). Maximum size is 1MB."
        )


def _set_secure_db_permissions():
    """
    Set restrictive permissions on database file (0600 = owner read/write only).

    This is critical for protecting sensitive data stored in SiteConfig and
    CommonSiteConfig tables (DB credentials, Redis URLs, API keys).
    """
    if DB_PATH.exists():
        try:
            # Set permissions to 0600 (rw-------)
            DB_PATH.chmod(0o600)
        except (OSError, PermissionError):
            # On Windows or restricted filesystems, chmod may fail
            # Still proceed but permissions may not be as strict
            pass


def _migrate_bench_label_column():
    """Add the ``bench.label`` column to pre-existing caches that lack it.

    ``create_tables(safe=True)`` only creates missing tables; it never adds a
    column to an existing table. Caches written before the label feature have a
    ``bench`` table with no ``label`` column, so we add it in place. Existing rows
    get NULL (no user label -> numeric index only), which keeps old caches fully
    working. This is idempotent: it is a no-op once the column exists.
    """
    try:
        columns = {row[1] for row in db.execute_sql("PRAGMA table_info(bench)").fetchall()}
        if "bench" not in db.get_tables():
            return
        if "label" not in columns:
            db.execute_sql("ALTER TABLE bench ADD COLUMN label VARCHAR")
    except Exception as e:  # pragma: no cover - defensive; never block on migration
        print(f"Warning: could not migrate bench.label column: {e}", file=sys.stderr)


def _migrate_bench_current_site_column():
    """Add the ``bench.current_site`` column to pre-existing caches that lack it.

    Same idempotent, in-place ``ALTER TABLE`` pattern as
    :func:`_migrate_bench_label_column` (``create_tables(safe=True)`` never adds a
    column to an existing table). Old caches get NULL, so they keep working and
    ``get_default_site`` falls back to ``common_site_config``'s ``default_site``
    until the next full inspect repopulates ``current_site`` from currentsite.txt.
    """
    try:
        columns = {row[1] for row in db.execute_sql("PRAGMA table_info(bench)").fetchall()}
        if "bench" not in db.get_tables():
            return
        if "current_site" not in columns:
            db.execute_sql("ALTER TABLE bench ADD COLUMN current_site VARCHAR")
    except Exception as e:  # pragma: no cover - defensive; never block on migration
        print(f"Warning: could not migrate bench.current_site column: {e}", file=sys.stderr)


def _scrub_cached_config_secrets():
    """One-shot: strip secrets from config rows written before redaction shipped.

    Caches created before ``_redact_config_for_cache`` stored the full site /
    common configs verbatim (db passwords, encryption_key, redis URLs). Re-filter
    every row through the same whitelist and rewrite ONLY the rows that actually
    change, so this is a no-op on fresh DBs and after the first run. Only
    ``config_json`` is touched - ``Project.last_updated`` is never bumped (these
    models carry no timestamp). Never raises: a row whose JSON won't parse is
    replaced with ``"{}"`` (a full inspect rebuilds it).
    """
    try:
        for model, allowed in (
            (CommonSiteConfig, _COMMON_CONFIG_CACHE_KEYS),
            (SiteConfig, _SITE_CONFIG_CACHE_KEYS),
        ):
            for row in model.select():
                try:
                    parsed = json.loads(row.config_json)
                except (ValueError, TypeError):
                    parsed = None
                cleaned = (
                    _redact_config_for_cache(parsed, allowed) if isinstance(parsed, dict) else {}
                )
                new_json = json.dumps(cleaned)
                if new_json != row.config_json:
                    row.config_json = new_json
                    row.save()
    except Exception as e:  # pragma: no cover - defensive; never block init
        print(f"Warning: could not scrub cached config secrets: {e}", file=sys.stderr)


def initialize_database():
    if db.is_closed():
        db.connect()
    # Create tables if missing
    db.create_tables(
        [Project, Bench, Site, AvailableApp, InstalledAppDetail, CommonSiteConfig, SiteConfig],
        safe=True,
    )
    # Backward-compatible schema migration for caches created before the label
    # feature. Must run after create_tables (so the table exists) and before any
    # read/write that references bench.label.
    _migrate_bench_label_column()
    # Same for the current_site column (added with the currentsite.txt default-site
    # resolution). Idempotent; NULL on old caches.
    _migrate_bench_current_site_column()
    # Retroactively strip secrets from configs cached before redaction shipped.
    # Idempotent no-op once every row is clean; must run after the tables exist.
    _scrub_cached_config_secrets()
    # Secure the database file with restrictive permissions
    _set_secure_db_permissions()


def clear_cache_for_project(project_name):
    initialize_database()
    try:
        project = Project.get(Project.name == project_name)
        project.delete_instance(recursive=True)
        return True
    except Project.DoesNotExist:
        return False


def clear_all_cache():
    if not db.is_closed():
        db.close()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


def cache_project_data(project_name, bench_instances_data):
    initialize_database()
    clear_cache_for_project(project_name)

    project = Project.create(name=project_name, last_updated=datetime.datetime.now())

    for bench_data in bench_instances_data:
        # Persist the user label when present. An empty string is normalized to
        # NULL so "no label" has a single representation in the DB.
        label_value = bench_data.get("label") or None
        # Normalize empty/missing currentsite pointer to NULL for a single
        # "no default recorded" representation.
        current_site_value = bench_data.get("current_site") or None
        bench = Bench.create(
            project=project,
            path=bench_data["path"],
            label=label_value,
            current_site=current_site_value,
        )

        # Store common site config if present (including empty configs)
        if "common_site_config" in bench_data:
            try:
                # Strip secrets BEFORE validating/storing so what is validated is
                # exactly what is persisted (see _redact_config_for_cache).
                redacted_common = _redact_config_for_cache(
                    bench_data["common_site_config"], _COMMON_CONFIG_CACHE_KEYS
                )
                _validate_config_json(redacted_common, "common_site_config")
                CommonSiteConfig.create(bench=bench, config_json=json.dumps(redacted_common))
            except (ValueError, TypeError) as e:
                # Log warning but continue - don't fail entire cache operation
                # User will still get other cached data
                print(
                    f"Warning: Skipping invalid common_site_config for bench {bench_data['path']}: {e}",
                    file=sys.stderr,
                )

        for app_name in bench_data["available_apps"]:
            AvailableApp.create(bench=bench, name=app_name)

        for site_data in bench_data["sites"]:
            site = Site.create(
                bench=bench,
                name=site_data["name"],
                installed_apps=json.dumps(site_data["installed_apps"]),
            )

            # Store site-specific config if present (including empty configs)
            if "site_config" in site_data:
                try:
                    redacted_site = _redact_config_for_cache(
                        site_data["site_config"], _SITE_CONFIG_CACHE_KEYS
                    )
                    _validate_config_json(redacted_site, f"site_config for {site_data['name']}")
                    SiteConfig.create(site=site, config_json=json.dumps(redacted_site))
                except (ValueError, TypeError) as e:
                    # Log warning but continue - don't fail entire cache operation
                    print(
                        f"Warning: Skipping invalid site_config for {site_data['name']}: {e}",
                        file=sys.stderr,
                    )

            # parse and store detailed installed app info
            for app_entry in site_data["installed_apps"]:
                parts = app_entry.split(maxsplit=2)
                if len(parts) == 3:
                    app_name, version, branch = parts
                else:
                    app_name = parts[0]
                    version = parts[1] if len(parts) > 1 else ""
                    branch = parts[2] if len(parts) > 2 else ""
                InstalledAppDetail.create(
                    site=site,
                    name=app_name,
                    version=version,
                    branch=branch,
                )


def get_cached_project_data(project_name):
    initialize_database()
    try:
        project = Project.get(Project.name == project_name)

        bench_instances_data = []
        # Order by primary key so benches come back in the same (sorted discovery)
        # order they were cached in. This makes each bench's position - its numeric
        # label / index - stable across reads (see utils/bench_labels.py).
        for bench in project.benches.order_by(Bench.id):
            available_apps = [app.name for app in bench.available_apps]

            # Get common site config for this bench
            common_config = None
            try:
                common_config_obj = CommonSiteConfig.get(CommonSiteConfig.bench == bench)
                common_config = json.loads(common_config_obj.config_json)
            except CommonSiteConfig.DoesNotExist:
                pass

            sites_info = []
            for site in bench.sites:
                site_data = {
                    "name": site.name,
                    "installed_apps": json.loads(site.installed_apps),
                }

                # Get site-specific config
                try:
                    site_config_obj = SiteConfig.get(SiteConfig.site == site)
                    site_data["site_config"] = json.loads(site_config_obj.config_json)
                except SiteConfig.DoesNotExist:
                    pass

                sites_info.append(site_data)

            bench_data = {
                "path": bench.path,
                "sites": sites_info,
                "available_apps": available_apps,
            }

            # Only surface a user label when one is set, mirroring how the optional
            # common_site_config is included. Absent key == no user label.
            if bench.label:
                bench_data["label"] = bench.label

            # Surface the default-site pointer (from currentsite.txt) when present,
            # so cache-served default-site resolution and the "(default)" marker work.
            if bench.current_site:
                bench_data["current_site"] = bench.current_site

            if common_config is not None:
                bench_data["common_site_config"] = common_config

            bench_instances_data.append(bench_data)

        return {
            "project_name": project_name,
            "bench_instances": bench_instances_data,
            "last_updated": project.last_updated,
        }
    except Project.DoesNotExist:
        return None


def set_bench_label(project_name: str, bench_path: str, label: str | None) -> bool:
    """Set (or clear) the user label of a single cached bench, by path.

    Used by the ``label`` command to update one bench without rewriting the whole
    project cache. An empty/None ``label`` clears it (stored as NULL). Returns True
    if a matching bench row was updated, False if the project or bench is not cached.
    """
    initialize_database()
    try:
        project = Project.get(Project.name == project_name)
    except Project.DoesNotExist:
        return False

    normalized = label or None
    updated = (
        Bench.update(label=normalized)
        .where((Bench.project == project) & (Bench.path == bench_path))
        .execute()
    )
    return bool(updated)


def get_all_cached_projects():
    initialize_database()
    return list(Project.select())


def get_common_site_config(project_name: str, bench_path: str | None = None) -> dict | None:
    """
    Get common_site_config for a project/bench.

    Args:
        project_name: Name of the project
        bench_path: Optional bench path to get config for specific bench

    Returns:
        Dictionary with common site config or None if not found
    """
    initialize_database()
    try:
        project = Project.get(Project.name == project_name)

        if bench_path:
            # Get config for specific bench
            bench = Bench.get((Bench.project == project) & (Bench.path == bench_path))
            try:
                config_obj = CommonSiteConfig.get(CommonSiteConfig.bench == bench)
                config: dict = json.loads(config_obj.config_json)
                return config
            except CommonSiteConfig.DoesNotExist:
                return None
        else:
            # Get config from first bench that has one
            for bench in project.benches:
                try:
                    config_obj = CommonSiteConfig.get(CommonSiteConfig.bench == bench)
                    config = json.loads(config_obj.config_json)
                    return config
                except CommonSiteConfig.DoesNotExist:
                    continue
            return None

    except (Project.DoesNotExist, Bench.DoesNotExist):
        return None


def get_site_config(
    project_name: str, site_name: str, bench_path: str | None = None
) -> dict | None:
    """
    Get site_config for a specific site.

    Args:
        project_name: Name of the project
        site_name: Name of the site
        bench_path: Optional bench path if multiple benches exist

    Returns:
        Dictionary with site config or None if not found
    """
    initialize_database()
    try:
        project = Project.get(Project.name == project_name)

        # Find the site
        if bench_path:
            bench = Bench.get((Bench.project == project) & (Bench.path == bench_path))
            site = Site.get((Site.bench == bench) & (Site.name == site_name))
        else:
            # Search all benches for the site
            site = None
            for bench in project.benches:
                try:
                    site = Site.get((Site.bench == bench) & (Site.name == site_name))
                    break
                except Site.DoesNotExist:
                    continue

            if not site:
                return None

        # Get the site config
        try:
            config_obj = SiteConfig.get(SiteConfig.site == site)
            config: dict = json.loads(config_obj.config_json)
            return config
        except SiteConfig.DoesNotExist:
            return None

    except (Project.DoesNotExist, Bench.DoesNotExist, Site.DoesNotExist):
        return None


def get_all_site_configs(project_name: str, bench_path: str | None = None) -> dict[str, dict]:
    """
    Get all site configs for a project or specific bench.

    Args:
        project_name: Name of the project
        bench_path: Optional bench path to limit to specific bench

    Returns:
        Dictionary mapping site names to their configs
    """
    initialize_database()
    result = {}

    try:
        project = Project.get(Project.name == project_name)

        benches = (
            [Bench.get((Bench.project == project) & (Bench.path == bench_path))]
            if bench_path
            else project.benches
        )

        for bench in benches:
            for site in bench.sites:
                try:
                    config_obj = SiteConfig.get(SiteConfig.site == site)
                    result[site.name] = json.loads(config_obj.config_json)
                except SiteConfig.DoesNotExist:
                    pass

        return result

    except (Project.DoesNotExist, Bench.DoesNotExist):
        return {}


def get_current_site(project_name: str, bench_path: str | None = None) -> str | None:
    """
    Get the default-site pointer (from currentsite.txt) cached for a project/bench.

    This is the fallback source for the default site when
    ``common_site_config.json`` has no ``default_site`` key. Returns the first
    non-empty ``current_site`` found (scoped to ``bench_path`` when given).
    """
    initialize_database()
    try:
        project = Project.get(Project.name == project_name)

        if bench_path:
            bench = Bench.get((Bench.project == project) & (Bench.path == bench_path))
            current: str | None = bench.current_site
            return current or None

        for bench in project.benches:
            current = bench.current_site
            if current:
                return current
        return None
    except (Project.DoesNotExist, Bench.DoesNotExist):
        return None


def get_default_site(project_name: str, bench_path: str | None = None) -> str | None:
    """
    Get the default site for a project/bench.

    A bench records its default site in EITHER ``common_site_config.json``'s
    ``default_site`` OR ``sites/currentsite.txt`` (the pointer ``bench use``
    writes). Resolve from the former first, then fall back to the latter (cached
    as ``current_site``) - a plain ``bench use``d dev bench only has the pointer.

    Args:
        project_name: Name of the project
        bench_path: Optional bench path to get default site for specific bench

    Returns:
        Default site name or None if not found
    """
    common_config = get_common_site_config(project_name, bench_path)
    if common_config and common_config.get("default_site"):
        return common_config.get("default_site")
    return get_current_site(project_name, bench_path)
