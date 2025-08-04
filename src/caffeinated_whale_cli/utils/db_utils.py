from peewee import Model, CharField, TextField, DateTimeField, SqliteDatabase, ForeignKeyField
from platformdirs import user_cache_dir
import os
import datetime
import json
from pathlib import Path

APP_NAME = "caffeinated-whale-cli"
CACHE_DIR = Path.home() / APP_NAME / "cache"
DB_PATH = CACHE_DIR / "cwc-cache.db"

os.makedirs(CACHE_DIR, exist_ok=True)
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
    alias = CharField(default="")


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


def initialize_database():
    if db.is_closed():
        db.connect()
    # Create tables if missing
    db.create_tables([Project, Bench, Site, AvailableApp, InstalledAppDetail], safe=True)
    # Ensure alias column exists for Bench
    cursor = db.execute_sql("PRAGMA table_info(bench)")
    cols = [row[1] for row in cursor.fetchall()]
    if 'alias' not in cols:
        # Add alias column in a transaction to ensure schema integrity
        with db.atomic():
            db.execute_sql("ALTER TABLE bench ADD COLUMN alias TEXT DEFAULT ''")


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
        bench = Bench.create(
            project=project,
            path=bench_data["path"],
            alias=bench_data.get("alias", ""),
        )

        for app_name in bench_data["available_apps"]:
            AvailableApp.create(bench=bench, name=app_name)

        for site_data in bench_data["sites"]:
            site = Site.create(
                bench=bench,
                name=site_data["name"],
                installed_apps=json.dumps(site_data["installed_apps"]),
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
        for bench in project.benches:
            available_apps = [app.name for app in bench.available_apps]
            sites_info = []
            for site in bench.sites:
                sites_info.append(
                    {"name": site.name, "installed_apps": json.loads(site.installed_apps)}
                )

            bench_instances_data.append(
                {
                    "path": bench.path,
                    "sites": sites_info,
                    "available_apps": available_apps,
                    "alias": bench.alias or "",
                }
            )

        return {
            "project_name": project_name,
            "bench_instances": bench_instances_data,
            "last_updated": project.last_updated,
        }
    except Project.DoesNotExist:
        return None


def get_all_cached_projects():
    initialize_database()
    return list(Project.select())


def set_bench_alias(project_name: str, bench_path: str, alias: str) -> bool:
    """
    Set or update the alias for a bench identified by project and bench path.
    Returns True if updated successfully, False if no matching bench was found.
    """
    initialize_database()
    try:
        project = Project.get(Project.name == project_name)
        bench = Bench.get((Bench.project == project) & (Bench.path == bench_path))
        bench.alias = alias
        bench.save()
        return True
    except (Project.DoesNotExist, Bench.DoesNotExist):
        return False


def clear_bench_alias(alias: str) -> bool:
    """
    Remove the alias from a bench entry matching the given alias.
    Returns True if alias was cleared, False if not found.
    """
    initialize_database()
    try:
        bench = Bench.get(Bench.alias == alias)
        bench.alias = ''
        bench.save()
        return True
    except Bench.DoesNotExist:
        return False


def get_bench_by_alias(alias: str):
    """
    Retrieve a single bench (with its sites and apps) by its unique alias.
    Returns a dict with project_name and bench data or None if not found.
    """
    initialize_database()
    try:
        bench = Bench.get(Bench.alias == alias)
        project = bench.project
        # Gather available apps
        available_apps = [app.name for app in bench.available_apps]
        # Gather sites
        sites_info = []
        for site in bench.sites:
            sites_info.append(
                {"name": site.name, "installed_apps": json.loads(site.installed_apps)}
            )
        return {
            "project_name": project.name,
            "bench": {
                "path": bench.path,
                "alias": bench.alias,
                "available_apps": available_apps,
                "sites": sites_info,
            },
        }
    except Bench.DoesNotExist:
        return None
