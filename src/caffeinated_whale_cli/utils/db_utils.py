import datetime
import json
import os
from pathlib import Path

from peewee import CharField, DateTimeField, ForeignKeyField, Model, SqliteDatabase, TextField

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
    """

    bench = ForeignKeyField(Bench, backref="common_config", unique=True)
    config_json = TextField()  # Stores the full JSON as text

    class Meta:
        table_name = "common_site_config"


class SiteConfig(BaseModel):
    """
    Stores site_config.json data for each individual site.
    Contains site-specific settings like database credentials.
    """

    site = ForeignKeyField(Site, backref="site_config", unique=True)
    config_json = TextField()  # Stores the full JSON as text

    class Meta:
        table_name = "site_config"


def initialize_database():
    if db.is_closed():
        db.connect()
    # Create tables if missing
    db.create_tables(
        [Project, Bench, Site, AvailableApp, InstalledAppDetail, CommonSiteConfig, SiteConfig],
        safe=True,
    )


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
        )

        # Store common site config if present
        if "common_site_config" in bench_data and bench_data["common_site_config"]:
            CommonSiteConfig.create(
                bench=bench, config_json=json.dumps(bench_data["common_site_config"])
            )

        for app_name in bench_data["available_apps"]:
            AvailableApp.create(bench=bench, name=app_name)

        for site_data in bench_data["sites"]:
            site = Site.create(
                bench=bench,
                name=site_data["name"],
                installed_apps=json.dumps(site_data["installed_apps"]),
            )

            # Store site-specific config if present
            if "site_config" in site_data and site_data["site_config"]:
                SiteConfig.create(site=site, config_json=json.dumps(site_data["site_config"]))

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

            if common_config:
                bench_data["common_site_config"] = common_config

            bench_instances_data.append(bench_data)

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


def get_common_site_config(project_name: str, bench_path: str = None) -> dict | None:
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
                return json.loads(config_obj.config_json)
            except CommonSiteConfig.DoesNotExist:
                return None
        else:
            # Get config from first bench
            bench = project.benches.first()
            if bench:
                try:
                    config_obj = CommonSiteConfig.get(CommonSiteConfig.bench == bench)
                    return json.loads(config_obj.config_json)
                except CommonSiteConfig.DoesNotExist:
                    return None
            return None

    except (Project.DoesNotExist, Bench.DoesNotExist):
        return None


def get_site_config(project_name: str, site_name: str, bench_path: str = None) -> dict | None:
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
            return json.loads(config_obj.config_json)
        except SiteConfig.DoesNotExist:
            return None

    except (Project.DoesNotExist, Bench.DoesNotExist, Site.DoesNotExist):
        return None


def get_all_site_configs(project_name: str, bench_path: str = None) -> dict[str, dict]:
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
