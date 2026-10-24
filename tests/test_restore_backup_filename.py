"""Backup-filename parsing for sites whose names contain hyphens."""

import pytest

from caffeinated_whale_cli.core import restore as core_restore
from caffeinated_whale_cli.core.restore import (
    group_and_sort_backups,
    parse_backup_filename,
    transform_site_name_to_backup_format,
)

TS = "20261025_022101"


@pytest.mark.parametrize(
    "site",
    [
        "my-site.localhost",
        "a-b-c.example.com",
        "x-private.localhost",
        "x-files.localhost",
        "x-database.localhost",
        "plain.localhost",
    ],
)
@pytest.mark.parametrize(
    "suffix,backup_type",
    [
        ("database.sql.gz", "database"),
        ("files.tar", "files"),
        ("private-files.tar", "private-files"),
        ("site_config_backup.json", "site_config_backup"),
    ],
)
def test_hyphenated_site_names_parse_with_the_owning_site_slug(site, suffix, backup_type):
    slug = transform_site_name_to_backup_format(site)
    parsed = parse_backup_filename(f"{TS}-{slug}-{suffix}", site_slug=slug)
    assert parsed is not None
    assert parsed["site_name"] == slug
    assert parsed["backup_type"] == backup_type


@pytest.mark.parametrize("site", ["my-site.localhost", "x-files.localhost", "x-database.localhost"])
def test_database_file_parses_without_a_hint(site):
    slug = transform_site_name_to_backup_format(site)
    parsed = parse_backup_filename(f"{TS}-{slug}-database.sql.gz")
    assert parsed is not None
    assert parsed["site_name"] == slug
    assert parsed["is_database"]


def test_private_files_ambiguity_defaults_to_the_shorter_site():
    # `foo-private-files` is site foo / private-files OR site foo-private / files.
    parsed = parse_backup_filename(f"{TS}-foo-private-files.tar")
    assert (parsed["site_name"], parsed["backup_type"]) == ("foo", "private-files")
    hinted = parse_backup_filename(f"{TS}-foo-private-files.tar", site_slug="foo-private")
    assert (hinted["site_name"], hinted["backup_type"]) == ("foo-private", "files")


@pytest.mark.parametrize(
    "name",
    [
        "notabackup.txt",
        f"{TS}-site.localhost.sql.gz",
        f"{TS}-site-unknowntype.tar",
        "20269999_999999-s-database.sql",
    ],
)
def test_non_backup_names_are_rejected(name):
    assert parse_backup_filename(name) is None


def test_hyphenated_site_backups_group_under_the_target_site():
    site = "x-private.localhost"
    slug = transform_site_name_to_backup_format(site)
    backups = []
    for suffix in ("database.sql.gz", "files.tar", "private-files.tar"):
        parsed = parse_backup_filename(f"{TS}-{slug}-{suffix}", site_slug=slug)
        parsed["site_dir"] = site
        parsed["full_path"] = f"/b/{parsed['filename']}"
        backups.append(parsed)
    target, other = group_and_sort_backups(backups, site)
    assert other == []
    assert len(target) == 1
    assert target[0]["site_name"] == slug
    assert target[0]["files"]["filename"].endswith("-files.tar")
    assert target[0]["private_files"]["filename"].endswith("-private-files.tar")


def test_scan_passes_the_directory_slug_so_a_private_suffix_site_is_not_misread():
    class Container:
        def exec_run(self, cmd, **_kw):
            text = cmd if isinstance(cmd, str) else " ".join(cmd)
            if "-type d" in text:
                return 0, b"/b/sites/x-private.localhost\n"
            if text.startswith("sh -c 'test -d"):
                return 0, b""
            return (
                0,
                (
                    f"/b/sites/x-private.localhost/private/backups/{TS}-x-private_localhost-files.tar\n"
                    f"/b/sites/x-private.localhost/private/backups/{TS}-x-private_localhost-database.sql.gz\n"
                ).encode(),
            )

    backups = core_restore.scan_backups_for_all_sites(Container(), "/b")
    by_type = {b["backup_type"]: b for b in backups}
    assert set(by_type) == {"files", "database"}
    assert by_type["files"]["site_name"] == "x-private_localhost"
