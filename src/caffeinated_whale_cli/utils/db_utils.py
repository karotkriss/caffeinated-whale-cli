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


class Site(BaseModel):
    bench = ForeignKeyField(Bench, backref="sites")
    name = CharField()
    installed_apps = TextField()


class AvailableApp(BaseModel):
    bench = ForeignKeyField(Bench, backref="available_apps")
    name = CharField()


def initialize_database():
    if db.is_closed():
        db.connect()
        db.create_tables([Project, Bench, Site, AvailableApp], safe=True)


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
        bench = Bench.create(project=project, path=bench_data["path"])

        for app_name in bench_data["available_apps"]:
            AvailableApp.create(bench=bench, name=app_name)

        for site_data in bench_data["sites"]:
            Site.create(
                bench=bench,
                name=site_data["name"],
                installed_apps=json.dumps(site_data["installed_apps"]),
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
                {"path": bench.path, "sites": sites_info, "available_apps": available_apps}
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


