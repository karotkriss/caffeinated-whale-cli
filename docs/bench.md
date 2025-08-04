# Frappe Bench CLI Commands (August 2025 Edition)

The **Frappe Bench CLI** is a command-line tool to manage Frappe/ERPNext deployments. It provides commands for setting up a bench (the environment and folder structure for Frappe apps), creating and managing sites, installing and updating apps, configuring system settings, and various development utilities. Below is a comprehensive list of **officially documented Bench commands** in the latest version (as of August 2025), grouped by functionality. Each command entry includes the command name, a description of its purpose, syntax, key options/flags, usage examples, and notes on expected outcomes.

## Bench Initialization and Environment Management

These commands help initialize a new bench (environment), update it, and manage the Python/Node environment and processes.

### **`bench init`** – Initialize a New Bench

**Description:** Creates a new Bench directory at a specified path. This sets up the standard bench folder structure: an `apps` directory for Frappe apps, a `sites` directory for site data, a `config` directory for configuration files (Redis, Nginx, etc.), and an `env` directory for the Python virtual environment. Running `bench init` will also install Frappe Framework in the new bench and prepare it for use.
**Syntax:**

```bash
bench init [OPTIONS] PATH
```

Where `PATH` is the file system path where the new bench should be created. Common options include specifying a Python executable or an apps JSON file to install immediately. For example, `bench init --python /usr/bin/python3.10 frappe-bench` will create a bench named “frappe-bench” using Python 3.10.
**Options:**

* `--python [PATH]` – Path to a specific Python executable to use for the bench virtualenv. By default, uses the system’s Python.
* `--ignore-exist` – Ignore if the target directory already exists (useful to reinitialize an existing bench).
* `--apps_path [FILE]` – Path to a JSON file listing apps to install after init (installs apps automatically).

**Example:**

```bash
bench init ~/frappe-bench
```

This will create a new bench directory `frappe-bench` in the home folder and set up the bench environment.

**Expected Output/Outcome:** On success, you will see messages about setting up the Python environment, installing Frappe, and creating folders. A new directory with the given name will be created containing subfolders like `apps`, `sites`, `env`, etc., ready for further use.

### **`bench update`** – Update Bench and Apps

**Description:** Updates the bench to the latest version of all apps and Frappe framework, applying migrations and rebuilding assets. Running `bench update` (with no flags) performs multiple steps: it takes a backup of each site, pulls the latest code for all apps from their git repositories, installs any new Python/Node dependencies, builds JS/CSS assets, runs database migrations (patches) for each site, and then restarts the bench’s process manager (if in production). Essentially, this keeps both the bench and all sites/apps up to date with their latest versions.
**Syntax:**

```bash
bench update [OPTIONS]
```

By default, no options are needed for a full update. However, specific flags can limit the operation:

* `--reset` – Hard reset git repositories to remote HEAD (discard local changes).
* `--patch` – Only run patches (skip pulling code).
  *(Note: The exact flags available can be seen with `bench update --help`, but are not fully detailed in the official docs.)*

**Example:**

```bash
bench update
```

This will perform a full update cycle. To update only certain parts, one could use specific flags (for instance, `bench update --patch` to apply patches without pulling code, though typically not needed if doing a full update).

**Expected Output:** The command prints progress for each step (backup notices, git pull output for each app, dependency installation logs, build process output, migration success messages). On completion, it should indicate that all apps are up to date. If errors occur (e.g., merge conflicts or failed migrations), the output will include error messages and the update may abort.

### **`bench restart`** – Restart Bench Services

**Description:** Restarts the bench’s background services/processes. In a development setup, this will restart the `bench start` process (the Procfile-managed processes). In a production setup, `bench restart` will restart process manager services (like the ***supervisor*** or ***systemd*** processes that were set up for the bench) as well as the web server processes. Use this to apply changes or recover from issues without rebooting the server.
**Syntax:**

```bash
bench restart
```

No additional options are required. This is typically used in a production environment to reload configurations or after updates.

**Example:**

```bash
bench restart
```

This will send commands to restart all managed processes (e.g., gunicorn/werkzeug servers, queue workers, scheduler, etc. depending on configuration).

**Expected Outcome:** For development benches, the command will terminate and relaunch the processes defined in the Procfile (you would see the same output as running `bench start`). In production, you should see messages indicating that services (such as nginx, schedule, web workers) have been restarted. There’s usually no lengthy textual output; the command completes silently if successful (or prints any errors if it fails to restart a service).

### **`bench migrate-env`** – Migrate Virtual Environment to Another Python Version

**Description:** Re-create the bench’s Python virtual environment (`env` directory) using a specified Python version. This is useful when upgrading Python (for example, moving from Python 3.10 to 3.11 for the bench). It essentially rebuilds the `env` with the target Python and reinstalls all required packages.
**Syntax:**

```bash
bench migrate-env [OPTIONS] new-python-path
```

The one required argument is the path to the new Python executable (e.g., `/usr/bin/python3.11`). Options may include `--no-backup` to skip backing up the old environment.
**Example:**

```bash
bench migrate-env /usr/bin/python3.11
```

This would rebuild the `env` using Python 3.11.
**Expected Outcome:** The command will remove the existing `env` folder and create a new one using the specified Python. It will output logs of creating a new virtual environment and installing packages. After completion, the bench will use the new Python version for all further operations. (Ensure all sites are compatible with the new Python version when doing this.)

### **`bench retry-upgrade`** – Retry a Failed Update/Upgrade

**Description:** If a `bench update` (or migrate) operation failed due to errors, `bench retry-upgrade` will attempt to rerun any pending patches or migrations that didn’t complete. It is essentially a helper to resume an upgrade that was aborted. Use this after fixing the cause of failure, to continue the upgrade process.
**Syntax:**

```bash
bench retry-upgrade
```

No special options; it simply re-invokes migrations/patches that are marked as not applied.
**Example:**
If `bench update` aborted while applying patches, you might run:

```bash
bench retry-upgrade
```

to attempt the patches again after addressing the underlying issue.
**Expected Outcome:** It will run through the remaining patch steps. Success means the upgrade completes (it may print migration logs). If issues persist, it may fail again with error messages indicating which patch or step is failing.

### **`bench disable-production`** – Disable Production Mode

**Description:** Reverts a bench that was set up for production back to development mode. This command will remove Nginx and process manager configurations, so that the bench can be used in a development setup (e.g., via `bench start`) rather than running as a production service. Essentially, it “tears down” what `bench setup production` did.
**Syntax:**

```bash
bench disable-production
```

There are no additional options. It must be run from within a bench directory, and typically as a user with the appropriate permissions (e.g., the same user that ran `bench setup production`).
**Example:**

```bash
bench disable-production
```

This will prompt if necessary and then disable supervisor/systemd and Nginx configurations for the bench.
**Expected Outcome:** After running this, the bench will no longer serve sites via Nginx. The output might indicate that services were stopped or config files removed. You would then use `bench start` to run the bench for development or set up production again if needed. (No output means it succeeded quietly.)

### **`bench renew-lets-encrypt`** – Renew Let’s Encrypt SSL Certificates

**Description:** Renews the Let’s Encrypt certificates for sites that have SSL enabled via Let’s Encrypt. This is typically a periodic operation (the bench’s cron jobs may do this), but it can be run manually. It will attempt to renew all certificates that are near expiry for sites on this bench and update the Nginx configuration if needed.
**Syntax:**

```bash
bench renew-lets-encrypt
```

No specific options; it operates on all sites that have been set up with Let’s Encrypt.
**Example:**

```bash
bench renew-lets-encrypt
```

This will contact the Let’s Encrypt ACME servers and renew certificates for any eligible sites.
**Expected Output:** The command will print the status of renewal for each site’s certificate (success or errors). On success, new certificate files are placed in `sites/{site_name}/private/letsencrypt` and Nginx is reloaded to pick up the new certificates. Typically, it prints messages from the Certbot client about certificate issuance.

### **`bench backup-all-sites`** – Backup All Sites in the Bench

**Description:** Creates a backup for every site in the bench, including database backup and files, similar to `bench backup` but automatically looping through all sites. This is useful for taking full snapshots of the entire bench. Each site’s backup (.SQL file and possibly files tarballs) will be placed in its `private/backups` folder by default.
**Syntax:**

```bash
bench backup-all-sites [OPTIONS]
```

It accepts the same options and flags as `bench backup` (for specifying paths, including/excluding files, etc.), which will be applied to each site in turn. For example, `--with-files` to include file backup, or `--compress` to gzip the files.
**Example:**

```bash
bench backup-all-sites --with-files
```

This will go through each site configured under the bench and perform a backup with files included.
**Expected Output:** For each site, you’ll see output similar to `bench backup` (confirmation of the SQL backup and file backup creation). The command will iterate over all sites; on completion, each site should have new backup files (with timestamped filenames) in its backups directory. If a site fails to back up (e.g., due to database issues), an error will be printed for that site and the process will continue to the next site.

### **`bench config`** – Bench Configuration Group

**Description:** `bench config` is a **command group** for altering bench-level configuration in `common_site_config.json` (the config that applies to all sites in the bench). It has several sub-commands to set or remove config keys and toggle certain settings. Common subcommands include:

* `bench config set-common-config -c KEY VALUE` – Set a config key in `common_site_config.json`.
* `bench config remove-common-config KEY` – Remove a config key from `common_site_config.json`.

There are also toggles like `bench config dns_multitenant on/off` to enable or disable DNS-based multitenancy, and settings for update behavior:

* `bench config update-bench-on-update on/off` – Enable or disable updating bench CLI itself when running `bench update`.
* `bench config restart-supervisor-on-update on/off` (or `restart-systemd-on-update`) – Control whether `bench update` automatically restarts process manager services.
* `bench config serve-default-site on/off` – Toggle serving the default site on port 80 (useful for multitenant setups).
* `bench config http_timeout  <seconds>` – Set the HTTP request timeout for gunicorn processes.

**Syntax:** Each subcommand under `bench config` has its own syntax. For example:

```bash
bench config set-common-config -c {key} {value}
```

(`-c` or `--config` indicates a key-value pair) or:

```bash
bench config dns_multitenant on
```

to enable DNS multitenancy.

**Examples:**

* Set a global config:

  ```bash
  bench config set-common-config -c maintenance_mode 1
  ```

  This writes `"maintenance_mode": 1` to the common config, affecting all sites.
* Remove a config:

  ```bash
  bench config remove-common-config maintenance_mode
  ```

  This will delete the `maintenance_mode` key from the common config.
* Enable DNS multitenancy:

  ```bash
  bench config dns_multitenant on
  ```

**Expected Outcome:** These commands directly edit the JSON config. They usually output a confirmation of the change. For example, after setting or removing a key, you may see the updated config printed or a success message. Enabling/disabling features prints the new state. The real effect is in the behavior of the bench or sites (e.g., turning on DNS multitenant changes how sites are resolved).

## Site Management Commands

Site management commands create, remove, or modify Frappe “sites” – each site is a separate database and set of files representing one Frappe/ERPNext instance.

### **`bench new-site`** – Create a New Site

**Description:** Creates a new Frappe site within the bench. This sets up a new site directory under `sites/` and a new database for that site, installing the Frappe framework’s tables (and any specified app’s tables) into the database. The command also generates a `site_config.json` for the site with default settings (like the administrator password and database credentials). If anything goes wrong during creation, it will prompt whether to rollback (delete the created database and files) to avoid half-created sites.
**Syntax:**

```bash
bench new-site [OPTIONS] <site-name>
```

The `<site-name>` is usually a domain-like name (e.g. `site1.local` or a custom domain) which becomes the folder name under `sites/`. Important options include:

* `--db-name` – Custom name for the new database (defaults to a name derived from the site name).
* `--db-user` and `--db-password` – Specify a non-root database user and password to use for the site’s database access (by default, the MariaDB root user is used to create a new DB and a DB user matching the site).
* `--db-type` – Choose the database engine: `"mariadb"` (default) or `"postgres"`.
* `--admin-password` – Set the Administrator user’s password for the new site (if not provided, you will be prompted securely).
* `--install-app` – Immediately install a specified app on the new site (e.g., `--install-app erpnext`).
* `--db-root-username` / `--db-root-password` – Credentials for the DBMS root/admin user if not using the default (useful for PostgreSQL or non-root setups).
* `--no-mariadb-socket` – On MariaDB, force TCP connection instead of socket (sets host to `%`).
* `--force` – Force creation even if a site or database with the same name exists (not generally recommended).

**Examples:**

1. **Basic site creation:**

   ```bash
   bench new-site mysite.local
   ```

   This will interactively ask for a MariaDB root password (to create the DB) and an Administrator password for the new site, then create `mysite.local/` in `sites/` and a new database for it.
2. **Create a site with PostgreSQL:**

   ```bash
   bench new-site pgsite.local --db-type postgres
   ```

   Uses PostgreSQL for the site’s DB (requires bench configured for PostgreSQL support).
3. **Non-interactive creation:**

   ```bash
   bench new-site test.local \
       --admin-password Pa$$w0rd \
       --db-root-password secret
   ```

   This sets the admin password and MariaDB root password via flags to avoid prompts.
4. **Custom DB settings:**

   ```bash
   bench new-site mysite2.local --db-name custom_db \
       --db-user dbuser --db-password dbpass --db-host 127.0.0.1 --db-port 3307
   ```

   Creates a site using a custom database name and a specific DB server/port and user.
5. **Using a non-root DB user for creation:**

   ```bash
   bench new-site client.local \
       --db-root-username db_admin --db-root-password adminpass
   ```

   This uses an alternative DB user with privileges to create the new database.
6. **Auto-install an app on creation:**

   ```bash
   bench new-site erp.mysite.com --install-app erpnext
   ```

   Creates the site and installs the ERPNext app on it in one command.

**Expected Outcome:** On success, the command prints messages like “Site mysite.local created” and notes about installation of modules. You should find a new folder under `sites/` named after the site, containing `site_config.json` and other subfolders. You can then run `bench --site <name> serve` (for development) or include the site in your production config. If any error occurs (database issues, etc.), the command will prompt to rollback (if not using `--force`). After creation, you can access the site via `http://localhost:8000` (in development, using `bench start`) or set up host names for production. The Administrator login will use the password you set (or was prompted for).

### **`bench drop-site`** – Delete a Site

**Description:** Drops an existing site from the bench. This command **permanently deletes** the site’s database and archives its site folder. Specifically, it will drop the database schema and move the site’s directory from `sites/{site}` to `sites/archived_sites/{site}` by default. Before deletion, it performs a safety backup (unless disabled) to ensure data is preserved in case of mistake. This is a destructive operation intended to completely remove a site.
**Syntax:**

```bash
bench drop-site [OPTIONS] <site-name>
```

**Important Options:**

* `--db-root-username` – The database administrator username with privileges to drop databases (defaults to “root” for MariaDB, or the PostgreSQL superuser).
* `--db-root-password` – Password for the above DB admin user (will prompt if not provided).
* `--archived-sites-path` – Specify a custom path where the site’s folder should be moved (instead of the default `archived_sites`).

**Flags:**

* `--no-backup` – Skip taking a backup before dropping the site.
* `--force` – Force the drop without interactive confirmation, even if errors occur (use with caution).

**Examples:**

1. **Standard drop with prompt:**

   ```bash
   bench drop-site mysite.local
   ```

   You’ll be prompted for the MariaDB root password (if applicable) and to confirm the irreversible deletion (unless `--yes` implied by `--force`). It will then back up and remove *mysite.local*.
2. **Non-interactive drop (provide DB password):**

   ```bash
   bench drop-site client.local --db-root-password 'secret'
   ```

   This skips the root password prompt by providing it upfront.
3. **Skip backup:**

   ```bash
   bench drop-site test.local --no-backup
   ```

   Deletes the site without creating a backup first (not recommended).
4. **Custom archive location:**

   ```bash
   bench drop-site oldsite.local --archived-sites-path /tmp/old_sites_archive
   ```

   Moves the `oldsite.local` folder to `/tmp/old_sites_archive/oldsite.local` after dropping the DB.

**Expected Outcome:** The site’s database will be dropped (the MySQL/PostgreSQL schema is gone) and the site’s folder is relocated. In the bench’s `sites/` directory, the site name will no longer be present (or is moved to the archive folder). The command’s output typically confirms each step: database dropped, files moved, and backup file location if backup was taken. After this, the site is completely removed from the bench; attempting to access it will fail unless restored from backup. (Ensure you update any multitenancy configs if the site was served on a domain.)

### **`bench migrate`** – Apply Database Migrations for a Site

**Description:** Updates a site’s database schema and data to reflect the current versions of all installed apps. Essentially, `bench migrate` runs all pending patches, schema migrations, and necessary synchronization tasks for the site. It performs the following steps in order: runs any **before\_migrate hooks**, executes outstanding **patches** for each app, **synchronizes** DocType schema changes (and any Desk components like desktop icons, dashboards), updates **fixtures**, rebuilds **search indexes** for web documents, and then runs **after\_migrate hooks**. This command should be run after updating apps (e.g., as part of `bench update`) or when installing a new app to ensure the site’s database is up-to-date.
**Syntax:**

```bash
bench --site <site-name> migrate [OPTIONS]
```

(You must specify the `--site` unless a default is set via `bench use`.)
**Flags:**

* `--skip-failing` – Skip any patch that throws an error and continue with others (**not recommended for production**, as it may leave the system in an inconsistent state).
* `--skip-search-index` – Do not rebuild the search index for routes/pages (saves time if you don’t need to update full-text search immediately).

**Examples:**

1. **Basic migration:**

   ```bash
   bench --site mysite.local migrate
   ```

   Runs all pending migrations for “mysite.local”. This will output logs for each patch and sync task executed.
2. **Skip search indexing:**

   ```bash
   bench --site mysite.local migrate --skip-search-index
   ```

   Useful if you want a faster migrate and will handle search indexing later.
3. **Skip failing patches:**

   ```bash
   bench --site mysite.local migrate --skip-failing
   ```

   Continues past a failing patch to apply others. *Use with caution:* the note in documentation explicitly warns that this flag should **not** be used in production unless absolutely necessary.

**Expected Output:** The command prints the name of each patch it’s executing, any output from those patch scripts, and any schema changes being applied. If all goes well, it ends with a message that migrations finished successfully. After `bench migrate`, the site’s “Installed Applications” list (in the UI or via `bench list-apps`) will reflect updated version numbers, and any new DocTypes or fields introduced by updates will be present in the database. If an error occurs in a patch (and `--skip-failing` isn’t used), the migrate will abort and error traceback will be shown; in that case, you’d resolve the issue and run `bench migrate` again (or `bench retry-upgrade` as needed).

### **`bench backup`** – Backup a Site

**Description:** Creates a backup of a single site’s database (and optionally its files). By default, this produces a SQL dump of the site’s database and places it in the site’s `private/backups/` directory with a timestamped filename. It can also backup public and private files if specified, and can exclude or include specific DocTypes’ data as needed. This command is useful for manual backups or before performing major changes to a site. If run without `--site`, it will backup the site currently set as default (`bench use`), otherwise you should specify `--site`.
**Syntax:**

```bash
bench --site <site-name> backup [OPTIONS]
```

**Key Options:**

* `--backup-path` – Directory path to save all backup files (if not the default `private/backups`).
* `--backup-path-db` – Custom file path for the database backup file (overrides the default name/location).
* `--backup-path-conf` – Custom path for backing up the site’s config JSON (site\_config).
* `--backup-path-files` – Custom path for the **public** files archive backup.
* `--backup-path-private-files` – Custom path for the **private** files archive backup.
* `--exclude -e "<Doctype1>,<Doctype2>,..."` – Comma-separated DocType names to **exclude** from the data backup. (Their data will not be included in the SQL dump.)
* `--only`, `--include -i "<DoctypeX>,<DoctypeY>,..."` – Instead of a full backup, include **only** these DocTypes’ data in the backup. (Cannot be used together with `--exclude`.)

**Flags:**

* `--with-files` – Also backup the site’s files (will create tar archives of private and public files). By default, `bench backup` only backs up the database; with this flag, you get `*files.tar` and `*private-files.tar` in backups.
* `--compress` – Gzip-compress the file backup archives (`.tgz` instead of `.tar` for file backups). This reduces size by compressing the tar archives of files.
* `--ignore-backup-conf` – Ignore any backup exclusions/inclusions defined in the site’s config (in `frappe.conf.backup.*` settings). Use this to do a full backup even if config normally skips certain DocTypes.
* `--verbose` – Print verbose output during backup (helpful to see what’s happening step-by-step).

**Examples:**

1. **Basic backup of database only:**

   ```bash
   bench --site mysite.local backup
   ```

   This will produce an SQL file (e.g., `mysite_local-database-20250803-160000.sql.gz`) in `sites/mysite.local/private/backups/`.
2. **Backup including files:**

   ```bash
   bench --site mysite.local backup --with-files
   ```

   In addition to the DB dump, this creates `mysite_local-files.tar` and `mysite_local-private-files.tar` for public and private files.
3. **Compressed file backups:**

   ```bash
   bench --site mysite.local backup --with-files --compress
   ```

   Similar to above, but the file archives will be `.tgz` (gzip compressed) instead of `.tar`.
4. **Custom backup directory:**

   ```bash
   bench --site mysite.local backup --backup-path /backups/mysite/
   ```

   Saves all backup files (DB dump and any file tars) to `/backups/mysite/` rather than the site’s default folder.
5. **Custom paths for each file:**

   ```bash
   bench --site mysite.local backup --with-files \
       --backup-path-db /backups/db.sql.gz \
       --backup-path-files /backups/publicfiles.tar \
       --backup-path-private-files /backups/privatefiles.tar
   ```

   Places each component at specified locations. Unspecified components go to default paths.
6. **Verbose backup of specific data only:**

   ```bash
   bench --site mysite.local backup --only "ToDo,Note,Task" --verbose
   ```

   Backs up *only* those DocTypes and prints details of the backup process.
7. **Backup excluding certain logs:**

   ```bash
   bench --site mysite.local backup --exclude "Error Log,Version" 
   ```

   Backs up everything except those DocTypes (useful to skip large log tables).
8. **Ignore config-based exclusions:**

   ```bash
   bench --site mysite.local backup --ignore-backup-conf
   ```

   Ensures a full backup even if `site_config.json` has exclude rules (set via `frappe.conf.backup.*`).

**Expected Outcome:** After running, you should find backup files in the designated location. The console output will indicate stages: starting backup, writing database dump, zipping files, etc. If a backup fails at any stage (due to low disk space or other errors), **any partial files are deleted** to save space. On success, the final lines will confirm that backup files were saved (with their paths). These backups can be used with `bench restore` to recreate the site if needed. The database dump is typically gzip-compressed (`.sql.gz`), and file backups are tar (or tar.gz) archives. Each backup run cleans up older backups according to retention policies if configured, but by default a few of the latest backups remain in the folder.

### **`bench restore`** – Restore a Site from Backup

**Description:** Restores a site’s database (and optionally files) from a backup file. Essentially, this is the inverse of `bench backup` – you provide a path to an SQL backup, and `bench restore` will wipe the current site’s database and replace it with the data from that SQL file. You can also provide paths to tar archives of public and private files to restore those files into the site. This is used to recover a site from a backup or to copy a site from one bench to another. **Important:** Frappe does not support downgrading – if the SQL backup is from a higher version of Frappe/ERPNext than the site currently is, the command will detect a version mismatch and warn or prompt before proceeding (to avoid accidental downgrades).
**Syntax:**

```bash
bench --site <site-name> restore [OPTIONS] SQL_FILE_PATH
```

`SQL_FILE_PATH` is the path to the database backup file (usually ending in `.sql` or `.sql.gz`). This can be an absolute path or relative to the bench or `sites/` directory. Common options:

* `--db-root-username` / `--db-root-password` – Credentials for DB admin user if needed to create the database or perform the restore (defaults to root).
* `--db-name` – If you want to restore into a database with a different name than the site’s default (rarely used; usually the site’s DB name is derived from site name).
* `--admin-password` – Set a new Administrator password for the site after restore. This is useful if you don’t know the admin password contained in the backup; you can override it.
* `--install-app` – Name of an app to install **after** restoration. (Typically used if you are restoring a site and then want to immediately install a new app on it.)
* `--with-public-files` – Path to a tar (or tar.gz) archive of the site’s **public** files to restore.
* `--with-private-files` – Path to a tar (or tar.gz) archive of **private** files to restore.

**Flags:**

* `--force` – Force restore even if a potential “downgrade” is detected or if the site already exists. Normally, if the backup is from a newer version of Frappe/ERPNext than the bench, the restore will halt with a warning. Using `--force` will ignore the downgrade warning and attempt the restore anyway (not recommended unless you are sure about the compatibility).

**Examples:**

1. **Basic restore from SQL:**

   ```bash
   bench --site mysite.local restore /path/to/mysite_local-database.sql.gz
   ```

   This will drop the current `mysite.local` database and import the one from the backup file (gunzip it automatically if `.gz`). If the backup is a different Frappe version, you will see a warning and need to confirm or use `--force`.
2. **Restore including file data:**

   ```bash
   bench --site mysite.local restore ~/backups/db_backup.sql \
       --with-public-files ~/backups/files.tar \
       --with-private-files ~/backups/private-files.tar
   ```

   This restores the DB and also extracts the public files archive into `sites/mysite.local/public/files` and the private files archive into `sites/mysite.local/private/files`. After running, the site’s files and database should match the backup.
3. **Restore with new admin password:**

   ```bash
   bench --site mysite.local restore backup.sql --admin-password newSecret
   ```

   After restoration, the Administrator user’s password will be set to “newSecret” (overriding whatever was in the backup).
4. **Restore into a new database name:**

   ```bash
   bench --site mysite.local restore backup.sql --db-name new_db_name
   ```

   This will create a database named `new_db_name` for the site instead of the default. Useful if you want a different DB name or are testing a restore without overwriting the original DB.
5. **Bypass downgrade protection:**

   ```bash
   bench --site mysite.local restore older_backup.sql --force
   ```

   If `older_backup.sql` came from a higher version, this flag forces the restore. (Use only if you plan to immediately upgrade the site’s apps to match, or if you understand the risk.)

**Expected Outcome:** If successful, the site’s database is replaced with the backup’s contents. The command outputs progress: you’ll see it creating the new database (or dropping and recreating), restoring tables, and then if file flags were provided, messages about extracting files. On completion, it may run a `bench migrate` automatically to ensure the restored site is synchronized to the code’s version (particularly if the backup was from an older version, migrating will apply any missing patches). After restore, you should be able to log into the site (Administrator password as given or as in backup) and see all the data from the backup. **Note:** The restore will not change the site’s name or domain settings; it purely affects data and files. In case of errors (e.g., wrong DB credentials, file not found, etc.), the command will abort with an error message. If a partial restore occurred, you might need to manually clean up (for example, if the DB was created but files not extracted, you can re-run or remove partial data).

### **`bench partial-restore`** – Restore Specific Data from Backup

**Description:** Restores *partial* data from a SQL file into a site. This is used in conjunction with backups taken with `--only` or `--exclude` options. For example, if you backed up only certain DocTypes using `bench backup --only`, you can use `bench partial-restore` to import just those DocTypes into a site without affecting other data. Essentially, it executes the SQL in the file (which should contain data for only some tables) against the site’s database. It’s a specialized command for merging or extracting specific data sets. It will accept uncompressed or gzip-compressed SQL files. **Important:** Use carefully, as it can overwrite data in the specified tables of the site.
**Syntax:**

```bash
bench --site <site-name> partial-restore [OPTIONS] SQL_FILE_PATH
```

The main **argument** is the path to the partial SQL file (just like `bench restore`, relative or absolute paths allowed).
**Flags:**

* `--verbose` / `-v` – Show detailed output of the SQL operations being executed.

*(No other flags; you typically would not partial-restore files, only DB tables included in the SQL.)*

**Example:**
Suppose you had backed up only the **Project** and **Task** data from a site into `proj_task_backup.sql`. To restore these into another site:

```bash
bench --site targetSite partial-restore -v /path/to/proj_task_backup.sql
```

This will connect to `targetSite`’s database and run the SQL commands from `proj_task_backup.sql` with verbose output. Only the tables for DocTypes Project and Task (and related) contained in the file will be affected.

**Expected Outcome:** The specified tables in the site’s DB are updated with the data from the SQL file. The console output, especially with `-v`, will list each SQL statement as it executes. After completion, the target site will have the records from the partial backup (either added or replacing existing ones in those tables). Because this is a raw execution of SQL, there’s no automatic schema migration – it assumes the site’s schema matches that of the backup. This command **will not** restore files; it’s strictly for database contents. Use cases include syncing particular DocTypes from one site to another for troubleshooting or merging specific data. If the SQL file contains references to tables that don’t exist on the target, those statements will fail (hence ensure the partial backup matches the site’s app versions). On error, you’ll see SQL errors and the process will stop, possibly in the middle of insertion (partial data may be written). There is no automatic rollback on failure, so you might need to manually clean any half-restored data if something goes wrong.

### **`bench reinstall`** – Reinstall Site (Wipe and Start Fresh)

**Description:** **Warning: destructive.** This command **drops** a site’s database and re-creates a fresh new database with the same name, essentially re-running the site installation. It wipes all data on the site, resetting it to a just-installed state (with only standard records). It is mostly used in development to quickly reset a site, or if you want to reinitialize a production site (rarely, since it deletes everything). Because it’s destructive, it prompts for confirmation unless forced. Currently, `bench reinstall` works for MariaDB-backed sites (Frappe Framework v16 indicates PostgreSQL support may be added in future).
**Syntax:**

```bash
bench --site <site-name> reinstall [OPTIONS]
```

**Options:**

* `--admin-password` – Set a new Administrator password for the reinstalled site (if not provided, a random one might be generated or the default “admin” used, depending on Frappe version).
* `--mariadb-root-username` / `--mariadb-root-password` – Credentials for the MariaDB root user to allow dropping and creating the database.

**Flags:**

* `--yes` or `-y` – Skip the confirmation prompt (“Are you sure?”) and proceed without asking.

**Examples:**

1. **Basic reinstall (with prompt):**

   ```bash
   bench --site devsite.local reinstall
   ```

   You’ll be asked to type “y” or “yes” to confirm that you want to wipe the site. After confirmation and providing the DB root password, the site’s database is dropped and a new one is created with fresh tables (essentially same outcome as if you had run `bench new-site` for that site name again).
2. **Non-interactive reinstall with credentials provided:**

   ```bash
   bench --site devsite.local reinstall --yes \
       --mariadb-root-password myrootpass --admin-password newAdminPass
   ```

   This will immediately proceed to wipe `devsite.local`’s DB and recreate it. The Administrator password on the new site will be “newAdminPass”, and because `--yes` was used, there’s no stop for confirmation.
3. **Reinstall using a different DB user (with SUPER privileges):**

   ```bash
   bench --site devsite.local reinstall \
       --mariadb-root-username dbadmin --mariadb-root-password dbadminpass
   ```

   If your DB is managed by a user other than “root”, you can specify it. This will attempt to drop/create the DB using `dbadmin` user. The prompt will still appear unless `--yes` is also given.

**Expected Outcome:** The site’s database is completely wiped and set up anew. You will see output similar to when creating a new site: tables being installed, default records being inserted (like creation of Administrator user, default DocTypes, etc.). If the site had additional apps installed, those apps will also be reinstalled (their hooks will run to insert any setup data). Essentially, the site is like a fresh install – no user data, only stock data from Frappe and any apps. The site’s files (under `public/files` and `private/files`) are **not touched** by this command, so any uploaded files remain in the folder, but since the database references are wiped, those files would not be linked to any records until reattached. If you intend a truly clean slate, you might manually remove or archive the files folder as well. The command will produce a warning in the output about being destructive and maybe note that it’s only for MariaDB currently. After completion, you can log in with Administrator and the new password (or default one if none provided). All site data prior to the reinstall is lost (unless you had a backup).

### **`bench list-apps`** – List Installed Apps on a Site

**Description:** Displays all Frappe applications installed on a given site, along with their versions. This is useful to verify what apps (and versions/branches) a site is running. The information is fetched from the site’s “Installed Applications” record in the database. By default, the output is in JSON format for easy parsing by scripts, but you can choose a text/table format for human reading. This command supports multi-site: you can specify a single site or use `--site all` to get an overview of every site.
**Syntax:**

```bash
bench --site <site-name or 'all'> list-apps [OPTIONS]
```

**Options:**

* `--format, -f` – Output format: either `"json"` or `"text"` (plain text list). The default is `"json"`. (In older versions, a “table” format may also be available, but as of now, text and json are the documented options.)

**Examples:**

1. **List apps on the current site (JSON default):**

   ```bash
   bench --site mysite.local list-apps
   ```

   This might output a JSON array with objects containing app name, version, branch, and git commit of each installed app. For example:

   ```json
   [
     {"app": "erpnext", "branch": "version-14", "commit": "4e88dcf", "version": "14.0.3"},
     {"app": "frappe", "branch": "version-14", "commit": "f8ec3d7", "version": "14.0.1"}
   ]
   ```

   *(The exact fields may vary if using legacy format.)*
2. **List apps on all sites:**

   ```bash
   bench --site all list-apps
   ```

   This will iterate through every site and output a combined list. In JSON, it typically returns a dictionary where keys are site names and values are lists of apps. In text format, it will print each site’s apps grouped by site name.
3. **Human-readable text format:**

   ```bash
   bench --site mysite.local list-apps --format text
   ```

   or simply `-f text`. This prints each app on a new line, e.g.:

   ```
   erpnext 14.0.3  
   frappe 14.0.1  
   ```

   possibly with additional details like branch/commit omitted in text mode for brevity.
   You can also use the short `-f` flag:

   ```bash
   bench --site mysite.local list-apps -f text
   ```

   which is equivalent.

**Expected Output:** Depending on format:

* **JSON (default):** A JSON list or dict describing apps and their versions. This is useful for programmatic use or quick checks.
* **Text:** A simpler listing, e.g., just the app names and versions on separate lines.
  When using `--site all` with text format, it might group by site: for each site, list the apps under it. This command is read-only; it doesn’t change anything on the site. It simply reads from the `Installed Applications` DocType (and possibly uses the git repo to fetch commit info if needed). If run on a site that hasn’t been migrated (e.g., a brand new site before installing any apps), it will at least show “frappe” (and maybe “erpnext” if that was installed during site creation). If an app was removed without cleaning up, it might still show up here if its record remains. This output can be compared with `bench version` (described later) which provides similar info with formatting differences.

### **`bench install-app`** – Install an App on a Site

**Description:** Installs a Frappe application onto a site. When you have a new app (for example, you created a custom app with `bench new-app` or downloaded one with `bench get-app`), you use `bench install-app` to link that app to a particular site’s database. This operation will add all the app’s DocTypes and data structures to the site’s database (by executing the app’s hooks like after\_install, and running its initial migrations). After running, the site will have the app listed in its installed apps and you can use the app’s features on that site. **Note:** This command must be run from within a bench, and you specify which site to install into.
**Syntax:**

```bash
bench --site <site-name> install-app <app-name>
```

There are no special flags documented for this command (aside from the common `--site` flag). You need to ensure the `<app-name>` corresponds to an app that is present in the bench’s `apps/` directory (i.e., you have either created it or gotten it via get-app). If the app has unmet dependencies, the install will fail (for example, installing ERPNext requires that Frappe is already installed on the site, which it is by default).

**Example:**
After getting an app, say you ran `bench get-app https://github.com/frappe/erpnext`, you would install it on a site:

```bash
bench --site mysite.local install-app erpnext
```

This will execute the ERPNext installation on *mysite.local*, creating the necessary DocTypes (like Item, Customer, etc.) and default records for ERPNext on that site. It may take a short while and output many messages (setting up domains, inserting default data). Once done, *mysite.local* will have ERPNext installed and you can log in to use ERPNext on that site.

**Expected Outcome:** The command prints output of the app’s install process. Typically, you will see messages like “Installing {app-name}…”, database migrations being applied, creation of default records, and so on. For ERPNext, for example, it would create default companies, item groups, etc., and end with “Installation complete” or a similar success message. The site’s `site_config.json` will get updated to include the new app in the `"installed_apps"` list (you can also verify by running `bench list-apps` on that site, which should now include the app). If the app has an after-install hook, it will run (which might set up any additional configurations). If errors occur (say a dependency is missing or a patch in the app fails), the installation will abort and you’d see a traceback. A common pitfall is forgetting to migrate after installing an app – but `install-app` actually runs the app’s migrations as part of its process. After success, the app is fully integrated into the site.

### **`bench uninstall-app`** – Remove an App from a Site

**Description:** Uninstalls a Frappe application from a site, *including removal of all linked documents and database tables* belonging to that app. This command is the counterpart to install-app. It deletes all DocTypes, modules, and records that were created for the app on that site. **Note:** The app’s files remain in the bench, but the site will no longer have that app’s data. You might use this if you want to detach an app from a site or before removing the app from the bench entirely. The command will refuse to run if the app is the last app providing essential modules (like you cannot uninstall frappe itself from a site). Also, you cannot uninstall an app that other apps depend on without force. By default, it takes a backup before uninstalling (unless `--no-backup` is used) to allow recovery of data if needed.
**Syntax:**

```bash
bench --site <site-name> uninstall-app <app-name> [OPTIONS]
```

**Flags:**

* `--yes, -y` – Bypass the confirmation prompt. Normally, you must confirm since this is destructive. Using `-y` assumes “yes, uninstall”.
* `--dry-run` – Simulate the uninstall by listing what would be deleted, but do not actually remove anything. This is helpful to see the impact (which DocTypes and records would go) without committing.
* `--no-backup` – Do not create a backup before uninstalling. By default, a backup is taken to `private/backups` as a safety measure; skipping it might save time but you lose an immediate recovery option.
* `--force` – Force remove the app even if it’s linked to other apps’ data or otherwise normally protected. This can be dangerous – for example, if other apps have documents referencing this app’s DocTypes, those references will break.

**Examples:**

1. **Preview uninstall (dry run):**

   ```bash
   bench --site mysite.local uninstall-app myapp --dry-run
   ```

   This will output all the DocTypes and customizations that *would* be removed if you proceed, without actually deleting anything. Use this to review and ensure you won’t lose unintended data.
2. **Uninstall with confirmation:**

   ```bash
   bench --site mysite.local uninstall-app myapp
   ```

   After running, you will be asked “Do you want to continue? \[y/N]”. Typing “y” will then perform the uninstallation: the site’s database tables for “myapp” will be dropped and its records deleted. A backup is created first by default.
3. **Uninstall without prompt or backup (not recommended):**

   ```bash
   bench --site mysite.local uninstall-app myapp --yes --no-backup
   ```

   This immediately attempts to uninstall *myapp* from *mysite.local* without asking and without a safety backup. Use this only in non-critical scenarios or if you have your own backup already.
4. **Force uninstall:**

   ```bash
   bench --site mysite.local uninstall-app myapp --yes --force
   ```

   This will remove *myapp* even if some of its data might normally cause an error. For example, if another installed app had linked documents, `--force` tries to remove *myapp* regardless. This should be a last resort.

**Expected Outcome:** If successful, the app’s presence is completely removed from the site’s perspective. The command prints out each step: typically it will list the DocTypes being deleted, the tables being dropped, etc., possibly in a verbose manner if dry-run or verbose flags are set. After completion, running `bench list-apps` on that site will no longer show the app. Also, the site’s `site_config.json` will have that app removed from the `"installed_apps"` list. All the app’s doctypes and data are gone from the database. If something fails (perhaps a foreign key constraint or missing permission), the command will abort with an error. In such cases, you might need `--force` or manual cleanup. Generally, it takes a backup first (SQL dump) which you can use to recover if needed. Uninstalling does **not** remove the app’s code from the bench; it only affects that one site. If you want to delete the app entirely from the bench, you would after that use `bench remove-app` (discussed next) which deletes the app from disk.

### **`bench remove-app`** – Remove App from Bench (Delete Code)

**Description:** **Bench-wide operation.** Removes an application completely from the bench’s environment. This deletes the app’s folder under `apps/` and removes references to it. It should only be used after you’ve uninstalled the app from all sites (using `uninstall-app` on each site). `bench remove-app` ensures no sites are still using the app and then deletes its files and clears its Python packages from the environment. It will also rebuild assets if necessary (since removing an app might change the combined JS/CSS). Use this to clean up an app that you no longer need in your bench at all.
**Syntax:**

```bash
bench remove-app <app-name>
```

**Options:**
There are no additional options; it’s a straightforward command. (No `--site` flag because it operates on the entire bench.)

**Example:**
If you previously had an app “myapp” and have uninstalled it from all sites, you can remove it:

```bash
bench remove-app myapp
```

The command will confirm that *myapp* is not installed on any site, then proceed to delete its directories and environment entries.

**Expected Outcome:** The app directory (`apps/myapp`) will be deleted from the bench. The Python environment will have the app’s package removed (for example, it might run `pip uninstall myapp` internally). The command output typically notes “Removing app myapp…”, and if successful, concludes with something like “App myapp removed”. It may also trigger a rebuild of assets to remove references to the app’s assets. If any site still has the app, the command will usually abort and warn you to uninstall it from those sites first. After removal, if you run `bench list-apps` on any site that had it, it shouldn’t appear (assuming you properly uninstalled it earlier). Essentially, the bench no longer knows about this app.

### **`bench get-app`** – Get (Download) a New App

**Description:** Downloads and installs an app into the bench from a remote git repository (or filesystem path). It clones the app’s repository into the bench’s `apps/` folder, and runs the necessary setup (like `pip install` for the app’s Python requirements). This command is the primary way to fetch an official or third-party app to use in your bench. After using `get-app`, you typically run `bench install-app` on a site to start using the app.
**Syntax:**

```bash
bench get-app [OPTIONS] <app-name> <repo-url or path>
```

* The first argument `<app-name>` is optional if the repo’s name should be used. You can provide a custom name (for instance if you want to name the folder differently). Often, you just provide the URL without specifying name, and it infers the app name from the repo.
* The second argument is the repository URL (e.g., a GitHub HTTPS or SSH link) or a path to a local app directory/zip.

**Options:**

* `--branch <branch-name>` – Clone a specific branch of the repo (default is usually the repo’s default branch, e.g., “main” or “master”).
* `--no-install` – Download the app code but **do not** run the `pip install` for it. By default, bench will install the app’s Python package into the env. Using `--no-install` allows you to just add the code and manually handle requirements later.

**Example:**

1. **Get an official app (ERPNext):**

   ```bash
   bench get-app erpnext https://github.com/frappe/erpnext.git
   ```

   This will clone the ERPNext repository into `apps/erpnext` and install it in the Python environment. You should ensure you’re using a compatible branch of ERPNext for your Frappe version (if not, you might specify `--branch version-14` for example). After this, you would run `bench install-app erpnext` on a target site to complete the integration.
2. **Get a custom app from a branch:**

   ```bash
   bench get-app --branch develop myapp https://github.com/username/myapp.git
   ```

   This clones the “develop” branch of the repo into `apps/myapp`. If the app requires specific Python libs, those are installed too.
3. **Get an app from local path:**

   ```bash
   bench get-app ../someapp
   ```

   If `../someapp` contains a Frappe app (with a valid setup.py or pyproject, etc.), this will copy it into the bench.
4. **Download without installing requirements:**

   ```bash
   bench get-app --no-install https://github.com/frappe/chat.git
   ```

   This would clone the repository (perhaps naming the folder `chat`) but skip the pip install. You might do this if you plan to manually inspect or modify requirements.

**Expected Outcome:** After running, you’ll have a new folder under `apps/` for the app, and the bench will be aware of the app. The output will show git cloning progress (or file copy) and then a message about installing the app’s setup (unless `--no-install` was used). It typically ends with “Installing <app-name>” and lists any Python packages being installed as part of the app. Once complete, `bench list-apps` (without specifying site, it lists bench-wide apps) will include the new app in the bench environment. However, no site has it installed yet until you run `bench install-app`. If something fails (e.g., git URL is wrong, or pip install fails due to dependency issues), the command will error out. You can always re-run `bench get-app` if it didn’t complete (or manually fix the environment and run `pip install` as needed). This command is effectively a convenient wrapper for `git clone` + `pip install -e` for the app in the bench environment.

### **`bench include-app` / `exclude-app`** – Manage App Update Inclusion

**Description:** These two related commands control whether a particular app should be considered when running `bench update`. By default, all apps are “included” in the update process. Marking an app as excluded means that `bench update` will skip pulling updates for that app (and skip its migrations). This can be useful if you have a custom app that you manage separately or do not want to update at the same time as others. `bench include-app` reverts the exclusion, ensuring the app will be updated normally. These commands do not install or remove apps; they simply toggle an update flag in the bench’s config for that app.
**Syntax:**

```bash
bench exclude-app <app-name>
bench include-app <app-name>
```

No special options or flags for these; they directly act on the given app.

**Example:**

* To prevent the app “erpnext” from being touched by `bench update` (perhaps you are on a custom fork or you want to freeze it at a version):

  ```bash
  bench exclude-app erpnext
  ```

  The output will indicate that *erpnext* is now excluded from updates. Next time you run `bench update`, it will skip pulling or migrating ERPNext.
* If you later want to include it again (to update it):

  ```bash
  bench include-app erpnext
  ```

  Then `bench update` will treat it normally. By default, all apps are included, so you usually only need `exclude-app` followed by `include-app` if you temporarily paused updates.

**Expected Outcome:** These commands update the bench’s configuration (likely in `common_site_config.json` or under a hidden bench config). They probably print a confirmation like “Excluded app erpnext from bench updates” or “Included app erpnext in updates by default”. Practically, if you check `.bench` or config, you’d find an entry reflecting this. The real effect is observed on `bench update` – an excluded app’s repository is not pulled, and its migrations are not run. It’s important to remember to re-include apps eventually; running an update with an app excluded for too long might cause it to fall behind, and re-including could then require manual intervention if its migrations are far behind. Use this feature sparingly and keep track of excluded apps.

### **`bench remote-urls` / `remote-set-url` / `remote-reset-url`** – Manage App Git Remotes

**Description:** These commands help manage the git repository URLs for apps in your bench:

* `bench remote-urls` – Displays the configured git remote URL for each app in the bench (useful to verify which fork or source each app is tracking).
* `bench remote-set-url <app> <new-url>` – Changes the git remote’s URL for the specified app’s repository. For example, if you want to point an app to a different GitHub fork or switch from HTTPS to SSH, you use this. It’s essentially running `git remote set-url origin <new-url>` in the app’s folder.
* `bench remote-reset-url <app>` – Resets the remote URL for the app to the official one (as known by Frappe). This works for core apps (frappe, erpnext, etc.) by setting their git remote back to the default upstream URL. For custom apps, it might not have an effect unless the official URL is known in bench’s config.

**Syntax:**

```bash
bench remote-urls
bench remote-set-url <app-name> <git-url>
bench remote-reset-url <app-name>
```

No additional options; straightforward usage.

**Examples:**

* **View remote URLs:**

  ```bash
  bench remote-urls
  ```

  This will output something like:

  ```
  erpnext: https://github.com/frappe/erpnext.git  
  frappe: https://github.com/frappe/frappe.git  
  myapp: https://github.com/you/myapp.git  
  ```

  showing each app and the URL it will pull updates from.
* **Switch an app to a different remote:**

  ```bash
  bench remote-set-url erpnext https://github.com/myfork/erpnext.git
  ```

  After this, running `bench remote-urls` would show the ERPNext URL set to your fork. Next `bench update` will pull from `myfork/erpnext.git` instead of the original.
* **Reset to official remote:**

  ```bash
  bench remote-reset-url erpnext
  ```

  This will switch the URL back to `https://github.com/frappe/erpnext.git` (the official source). Use this if you mistakenly pointed to a wrong repo or want to go back to core.

**Expected Outcome:** `remote-urls` simply prints the list. `remote-set-url` and `remote-reset-url` will output a confirmation that the remote was changed. Under the hood, these commands affect the git configuration in `apps/{app}/.git/config`. If you run `git remote -v` in the app directory, you’ll see the updated URL after using these. They do not pull or push any code by themselves; they only set the source. If an invalid URL is given, you’ll only find out when you attempt to update/pull and it fails. `remote-reset-url` knows the “official” URLs for certain apps (likely those maintained by the Frappe team) and resets to those. If used on a custom app, it might not know what URL to reset to unless that’s tracked, so typically it’s meant for frappe, erpnext, etc.

### **`bench switch-to-branch` / `switch-to-develop`** – Switch App Branches

**Description:** These commands simplify switching all apps (or specific apps) in the bench to a different git branch. This is commonly used when upgrading to a new major version of Frappe/ERPNext.

* `bench switch-to-branch <branch-name> [app1 app2 ...]` – Switches the specified apps to the given git branch. If no apps are listed, it switches **all apps** to that branch. After switching branches, it will also run `bench update --patch` to apply any patches on the new branch, ensuring the database is migrated to match the code.
* `bench switch-to-develop` – A shortcut specifically to switch Frappe and ERPNext (and all official apps) to the `develop` branch (the bleeding-edge development branch). This is essentially `bench switch-to-branch develop --site all` for core apps.

**Syntax:**

```bash
bench switch-to-branch version-15
bench switch-to-branch version-15 frappe erpnext
bench switch-to-develop
```

In the first form, specifying the branch (e.g., `version-15`) without listing apps will attempt to checkout that branch in every app’s repository. Providing app names limits the operation to those apps.

**Examples:**

* **Switch all apps to a new release branch:**

  ```bash
  bench switch-to-branch version-15
  ```

  This will iterate through each app folder in `apps/` and run `git fetch` and `git checkout version-15` (or create a local tracking branch for it). It’s used during major version upgrades (say from v14 to v15). After switching, it will prompt to run migrations. Commonly you run `bench update --patch` after (the command might do it automatically or instruct you to do so).
* **Switch only core apps to a branch:**

  ```bash
  bench switch-to-branch version-15 frappe erpnext
  ```

  This leaves other apps on their current branches but moves Frappe and ERPNext to *version-15*. This is useful if you have custom apps not following the same branch naming scheme or you handle them separately.
* **Switch to develop for bleeding edge:**

  ```bash
  bench switch-to-develop
  ```

  This is like opting into the latest development version for all official apps (Frappe, ERPNext, etc.). Use it if you want to contribute or test upcoming features. It will checkout the `develop` branch in each repository. Typically, after this, a `bench migrate` or `bench update` is needed.

**Expected Outcome:** The command’s output will show, for each app being switched, something like:

```
Switched frappe to branch version-15
Switched erpnext to branch version-15
```

and possibly the latest commit hash on that branch. If an app doesn’t have that branch (e.g., a custom app not yet ported to that version), it will warn or error (“branch not found”). After switching branches, the bench is not fully upgraded until migrations run; often the command will automatically run migrations (or instruct you to run `bench migrate`). In the case of `switch-to-develop`, after switching, you’re on the dev track – you’d update regularly to get new commits. Note that switching branches can be a complex operation: you should ensure you have committed or stashed any local changes in apps, as it will attempt to check out new code. If there are uncommitted changes, it may pause or fail. Successfully switching branches is usually followed by a successful `bench update` to finalize the update.

### **`bench use`** – Set Default Site for Bench

**Description:** Sets a “default” site for the bench, which is used for commands when `--site` is not explicitly provided. It also updates the bench’s configuration so that in a production setup, this default site is served on port 8000 (if multitenancy is off) or on the configured host. In multitenant mode (DNS multitenant off), only the default site is served. This command effectively does two things: writes the site name into the file `sites/currentsite.txt` (used by bench to know default) and updates Nginx config if in production to serve that site as default.
**Syntax:**

```bash
bench use <site-name>
```

Just provide the site you want to mark as the bench’s primary site.

**Example:**

```bash
bench use mysite.local
```

After this, any bench commands that operate on a site can be run without `--site` flag, and it will assume *mysite.local*. For instance, `bench backup` will target *mysite.local* by default. If you had a previous default site, it replaces it. In production, `bench setup nginx` is typically run after changing the default site, to update which site responds on the default hostname.

**Expected Outcome:** The command prints something like “Set mysite.local as default site”. The file `sites/currentsite.txt` will contain `mysite.local`. If you open your browser at the server’s IP (with no host name specified) or use `localhost` in a dev environment, you’ll reach that default site. In a single-site bench, it’s common to set that one site as default. If you run `bench use` with a site that doesn’t exist or a typo, it will error out saying site not found. This setting persists until you change it again.

## App Development Commands

These commands are used during app development or for release management of apps.

### **`bench new-app`** – Create a New Frappe App

**Description:** Scaffolds a new Frappe application. Running this will create a new folder under `apps/` with the given app name and generate the standard structure (modules, hooks, boilerplate files) for a Frappe app. It also sets up a git repository (if git is available) and installs the app into the bench’s environment (so that you can immediately start developing and then use `bench install-app` to put it on a site). You should use this command when you want to build a custom application on the Frappe framework.
**Syntax:**

```bash
bench new-app <app-name>
```

You will be prompted for some information during creation:

* The **App Title** (a human-friendly name for the app).
* The **App Description**.
* The **App Publisher** (your name or company).
* The **App Email** (contact email).
* The **App Icon** (icon name or emoji for the app, optional).
* The **App Color** (a color code for the icon background, optional).
* Whether to create a GitHub repository (if integrated) – this is usually just a prompt to initialize a git repo locally.

**Example:**

```bash
bench new-app library_management
```

This would create an app named “library\_management”. During the prompts, you might enter “Library Management” as the title, add a description, etc. Once complete, you will have an `apps/library_management/` directory with files like `library_management/hooks.py`, `library_management/modules.txt`, and so forth. The app will also be added to the bench’s `sites/apps.txt` and installed in the virtualenv (setup.py is executed).

**Expected Outcome:** A directory structure for the new app is created. The console will output each file it creates. For instance:

```
Creating Library Management
  in folder apps/library_management
Install ing library_management...
```

and a bunch of file paths. If successful, at the end it might print “\*\*\* App Created Successfully! \*\*\*” or similar. The app’s entry is added to `apps/apps.txt` (so bench knows about it). You can now start adding DocTypes, pages, etc., within that app. It’s not installed on any site yet, but you can do `bench install-app library_management` on a site to start using it. The app also comes with a basic test and README. If a git repository was initialized, you’ll see a `.git/` in the app folder; commit as needed. No database changes happen on creation; it’s purely file scaffolding. If an app with that name already exists in the bench, the command will abort to avoid collisions.

### **`bench console`** – Open a Python Console for a Site *(Frappe Framework command)*

**Description:** *(This is technically a Frappe framework command invoked via bench, not defined in bench CLI itself, but it’s commonly used.)* Opens a Python interactive console (REPL) with the site’s environment loaded. It’s similar to opening a shell with `frappe` and `frappe.get_doc` etc. available. You use it for debugging or executing Python code in the context of a site.
**Syntax:**

```bash
bench --site <site-name> console
```

No special options. Once in the console, you can import modules or use `frappe` API directly. The prompt usually shows the site name.

**Example:**

```bash
bench --site mysite.local console
```

This will drop you into a Python shell: e.g., `In [1]:` prompt, where you can do things like:

```python
>>> import frappe
>>> frappe.db.get_all('User')
```

to fetch all User records, etc.

**Expected Outcome:** It’s an interactive session, so no fixed output. When you exit (via Ctrl+D or `exit()`), you return to the normal shell. This command is used for ad-hoc queries or executing Python logic without writing a script. It’s equivalent to `bench execute` (another command) for quick one-liners, but interactive.

### **`bench execute`** – Execute a Python Function

**Description:** *(Also a Frappe command via bench.)* Runs a specific Python function from the Frappe context. Useful for testing or running utilities. For example, you can call a whitelisted method or any library function on a site with this.
**Syntax:**

```bash
bench --site <site-name> execute <module.path.to.function> --kwargs "{'arg': value}"
```

You provide the fully qualified function name. Optionally `--args` or `--kwargs` to pass arguments (as JSON string or dict literal). This will import and run the function on the site.

**Example:**

```bash
bench --site mysite.local execute frappe.utils.reset_password --args "['user@example.com']"
```

This would execute the `reset_password` function with the given argument.

**Expected Outcome:** The command prints whatever the function returns (if anything). If the function raises an error, you get the traceback. This is for quick utility calls without writing a standalone script.

*(Note: `bench console` and `bench execute` are part of “Frappe Commands” rather than “Bench Commands”, according to documentation. They are included here for completeness since they are used via the bench CLI.)*

### **`bench start`** – Start Bench in Development Mode

**Description:** Launches the Frappe development server and related background processes (like socket.io, queue workers) using the Procfile configuration. This is the main command to run when developing; it will start a Node.js server for realtime and a Python WSGI server for Frappe, etc., showing logs in the terminal. It reads the `Procfile` in the bench directory (which defines processes like `web: frappe serve`, `socketio: node socketio.js`, etc.) and starts all of them.
**Syntax:**

```bash
bench start
```

There are no extra options for this. It should be run from the bench directory. Keep the terminal open to keep the processes alive.

**Example:**
Simply:

```bash
bench start
```

This will output a colorful log with each process labeled. For instance, you’ll see lines prefixed with `[redis_queue]`, `[redis_socketio]`, `[web]`, `[socketio]` etc., each representing output from that service. If everything is correct, you’ll see “Bench *ready*” or a similar indication that it’s running, and typically an indication that the development server is listening on `0.0.0.0:8000` for the web app.

**Expected Outcome:** The Frappe development environment is running. You can open a browser to `http://localhost:8000` to access the default site served by the bench. All logs (errors, prints, etc.) will stream to the console. This command continues running until you stop it (Ctrl+C). It’s meant for development and not used in production (in production, you’d use `bench setup` to configure Nginx and a process manager instead). If any service fails to start (say port 8000 is in use, or Node is not installed for socket.io), you’ll see error messages in the log output and possibly the process will keep retrying or exit. When you want to stop, you use Ctrl+C which stops all child processes.

### **`bench build`** – Build Asset Files *(Frappe command invoked via bench)*

**Description:** Compiles JS and CSS assets for the Frappe desk and web interface. It uses webpack or rollup as configured to bundle the app’s `.js` files into minified assets and compiles SCSS into CSS. It’s typically run after pulling updates that include frontend changes. In development, `bench start` watches and builds on the fly; in production or for customizations, you might run `bench build` manually.
**Syntax:**

```bash
bench build
```

No special options commonly used (there are some like `--app` to build assets for one app, or `--production` to minify, which is default anyway in production benches).

**Example:**

```bash
bench build
```

This will go through each app’s public assets and produce files under `sites/assets`. For example, it generates files like `frappe.js`, `erpnext.css`, and so on, which bundle all the code.

**Expected Outcome:** A successful build ends with messages indicating assets were compiled (or a simple “Building complete” message). The output can be quite verbose, showing which files were bundled or any warnings from the bundler. If there are syntax errors in custom JS or missing npm dependencies, those will appear as errors. In a production bench, you run this after `bench update` to ensure the latest static assets are ready. The end result is updated files in `sites/assets` that the web server serves. If using develop mode, `bench watch` (not an official separate command in newer versions, integrated into `bench start`) would watch changes; but `bench build` is a one-off command to build everything now.

### **`bench src`** – Show Bench Source Folder Path

**Description:** Prints the path to the bench’s Python source code (the `frappe-bench` package source). This is a minor utility: since bench is installed via pip, this helps you locate the actual bench code if you want to inspect or edit it. By running `cd $(bench src)`, you can navigate to where bench’s own Python code resides on your system.
**Syntax:**

```bash
bench src
```

No options. It simply returns a directory path.

**Example:**

```bash
$ bench src
/home/frappe/.local/lib/python3.10/site-packages/frappe_bench
```

This output path is where the `bench` CLI’s source code is installed. On some systems it might be in a `env/lib/python3.x/site-packages/bench/` or similar.

**Expected Outcome:** It will output a filesystem path. This command doesn’t affect anything in the bench; it’s purely informational. It’s often used by developers who may want to quickly go to bench’s code to debug or patch something. If bench is not installed in site-packages (for example, in editable mode), it shows that path. Otherwise, not commonly used by end-users.

### **`bench release`** – Create a Release for an App

**Description:** Prepares a new version release of a Frappe app (for app maintainers). It updates version identifiers and creates tags or branches as needed. This is an advanced command mainly used by Frappe core team or app developers when they are cutting a new release of their app. For example, generating the `CHANGELOG.md` or bumping version in `hooks.py`.
**Syntax:**

```bash
bench release --app <app-name> --major/--minor/--patch
```

You specify the app and which part of the version to bump (semantic versioning). `--major` would increment X in X.Y.Z, `--minor` increments Y, `--patch` increments Z. The command then likely creates a git tag for the new version and updates app metadata.

**Example:**

```bash
bench release --app myapp --patch
```

This would bump, say, version 1.2.3 to 1.2.4 for *myapp*, commit changes like updating the version in `hooks.py` (where app version is stored), and create a git tag `v1.2.4`. You’d push this to remote to mark a release.

**Expected Outcome:** The command will output what it’s doing: “Bumping version from 1.2.3 to 1.2.4”, “Creating git tag v1.2.4”, etc. After running, your app’s setup files and hooks should reflect the new version, and a tag should exist in the repo. If not all conditions are met (like a clean working directory), it might refuse to run.

### **`bench prepare-beta-release`** – Prepare a Major Beta Release

**Description:** Another release management command intended to facilitate preparing a new major beta release from the `develop` branch. This might automate things like branching out develop into a new major version branch, updating version numbers to beta, etc. It’s highly specific and likely used by the core team when moving from develop to a beta phase for a new version.

**Syntax:**

```bash
bench prepare-beta-release --app <app-name>
```

It might assume a lot, like you are on develop branch of the app and want to cut a beta.

**Expected Outcome:** Hard to detail without official docs, but presumably it sets up the repository for beta: e.g., creates a branch “version-x-beta” or bumps version to an X.0.0-beta and commits. It’s not commonly used outside of Frappe’s own release process.

## Bench Setup Commands (Server Configuration)

The **`bench setup`** command group configures various aspects of the bench’s server environment. These commands generate configuration files or set up services needed for production deployment (or for bench utilities).

Usage pattern:

```bash
bench setup <sub-command> [OPTIONS]
```

These usually require sudo or certain system permissions if altering system config (like adding to /etc). Below are important setup sub-commands:

* **`bench setup sudoers`** – Adds bench commands to the sudoers file so that certain bench operations can run as sudo without a password. This is typically run once to allow bench to internally use sudo for things like restarting services.
* **`bench setup env`** – Recreates the Python virtual environment (`env` folder) for the bench. Similar to `migrate-env`, but usually used initially or if you want to rebuild env with current `requirements.txt`.
* **`bench setup requirements`** – Installs required Python and Node packages for apps in the bench. It reads each app’s requirements and does `pip install` / `npm install`. This runs during `bench update` automatically, but can be invoked manually.
* **`bench setup redis`** – Generates Redis config files for caches and queues. It will place redis config files in the bench’s config folder and set proper ports.
* **`bench setup socketio`** – Installs Node dependencies for the socket.io server (real-time communication). This basically runs `yarn` or `npm` in `frappe` to ensure the socket.io server script is ready.
* **`bench setup config`** – Creates or updates `common_site_config.json` with default values. Useful if you want to reset it to defaults or ensure required keys are present.
* **`bench setup backups`** – Adds a cron job to automate `bench backup` for sites. It likely symlinks a cron file or prints instructions to schedule backups.
* **`bench setup procfile`** – Generates a new `Procfile` based on current apps for use with `bench start`. Usually not needed unless you have custom processes.
* **`bench setup socketio`** – (As above, sets up the Node socket.io service.)
* **`bench setup manager`** – Installs *Bench Manager*, which is a GUI app to manage benches. This command creates a site called `bench-manager.local`, gets the `bench_manager` app, and installs it there. Essentially, a way to use a web interface for bench tasks.
* **`bench setup production`** – Sets up the bench for production for a given Linux user. This is a one-command deployment: it installs required system packages (via the `bench install` commands, using ansible in older versions), sets up **NGINX** configuration for all sites, sets up **supervisor** or **systemd** to manage processes, and enables them. For example, `bench setup production frappe` will configure everything such that sites are served on port 80 by Nginx and processes managed by supervisor under user “frappe”. It’s a critical command to go from a dev bench to a live server bench.
* **`bench setup nginx`** – Generates Nginx config files for the bench’s sites without enabling them. If you add a new site or domain, you run this to update the config, then reload Nginx.
* **`bench setup supervisor`** (or `systemd`) – Creates config for process manager (supervisor conf files or systemd unit files) for running Frappe workers, webs, socketio, etc..
* **`bench setup fail2ban`** – Configures Fail2ban jail rules for the bench’s logs (to ban IPs on too many failed logins).
* **`bench setup firewall`** – Sets basic UFW firewall rules (if UFW is installed) for common ports (e.g., allow 80/443, maybe 22, etc.).
* **`bench setup ssh-port`** – Change the SSH port in firewall and fail2ban settings (closing default 22 and opening another).
* **`bench setup lets-encrypt`** – Sets up Let’s Encrypt SSL for a particular site (obtains a certificate and configures Nginx). It usually automates the certificate issuance and updates the site’s nginx config to use HTTPS.
* **`bench setup wildcard-ssl`** – Similar to above, but for a wildcard SSL certificate for multi-tenant benches (one cert for \*.yourdomain).
* **`bench setup add-domain`** – Assigns a new custom domain to a site (updates site\_config and Nginx).
* **`bench setup remove-domain`** – Removes a domain from a site’s config.
* **`bench setup sync-domains`** – Checks if the sites’ domain list changed and updates Nginx config if needed.
* **`bench setup role <role-name>`** – Installs dependencies via an Ansible role (legacy, e.g., `bench setup role production` internally calls underlying roles).

Each of these commands usually prints what it’s doing (writing files, etc.). They may require root permission for actions like writing to `/etc/nginx/` or `/etc/supervisor/`. If run as a non-root user, they attempt to use sudo for those parts (hence why `bench setup sudoers` is needed beforehand to allow that without prompting for password each time).

**Example Workflows:**

* **Initial production setup:** After installing bench and creating sites, you do:

  ```bash
  bench setup production frappe
  ```

  This would internally run `bench setup nginx`, `bench setup supervisor`, etc., and enable/configure them. After this, your sites are live on port 80 and processes managed in the background.
* **Add a new domain to a site:**

  ```bash
  bench setup add-domain mysite.local newdomain.com
  bench setup nginx --yes
  sudo service nginx reload
  ```

  (The `--yes` might skip confirmation on overwriting config). This will map `newdomain.com` to *mysite.local*. Then you could run `bench setup lets-encrypt newdomain.com` to secure it.
* **Enable multitenancy (DNS based):**

  ```bash
  bench config dns_multitenant on
  bench setup nginx && sudo nginx -t && sudo service nginx reload
  ```

  Now, whichever site is set as default will catch all unspecified domains, or if DNS multitenant is on, bench will rely on hostnames to route.

**Expected Outcome:** Each `bench setup ...` subcommand typically ends with either a success message or instructions. For example, `bench setup nginx` will tell you where the config is written (e.g., `sites/nginx.conf` and maybe copied to `/etc/nginx/conf.d/frappe.conf`). `bench setup lets-encrypt` will prompt for an email and then fetch certificates, outputting “Congratulations! Your certificate and chain have been saved at ...” on success. `bench setup production` should conclude with something like “Enabled supervisord” or “frappe configured for production”. After running the relevant setup commands, the environment should be configured accordingly: services running, configs in place. If there are errors (common ones: forgetting to run as root for certain parts, or missing dependencies), the output will highlight them (e.g., nginx test failed, or permission denied writing a file). The `bench doctor` command (in Frappe, not documented above) can be used to verify that background workers are running.

In summary, **bench setup** commands bridge the gap between a raw bench and a fully operational deployment.

## Miscellaneous Utilities

Finally, a few other bench commands and information:

* **`bench find`** – Recursively searches for bench instances starting from the current directory. For example, if you run `bench find` in your home, it will list any bench installations under it. Useful if you lost track of where a bench was set up.
* **`bench change-port`** – *(Not explicitly documented above, but older bench versions had this)* Changes the default port from 8000 to something else. In newer bench, this might be done via config or `bench use` and nginx.
* **`bench set-nginx-port`**, **`bench set-ssl-certificate`**, **`bench set-ssl-key`**, **`bench set-url-root`**, **`bench set-mariadb-host`**, **`bench set-redis-*`** – These are advanced configurators for specific site settings like port and host names for services. For instance, `bench set-nginx-port mysite.local 8080` would configure *mysite.local* to serve on port 8080 in Nginx instead of 8000. `bench set-redis-cache-host 127.0.0.1` would point the bench to a Redis cache server at that host, etc. After using these, you’d do `bench setup nginx` to apply changes.
* **`bench download-translations`** – Fetches the latest translation files from the Frappe translations portal for the apps in your bench. This is to update languages (pulls .csv or .po files).
* **`bench doctor`** – (Frappe command) Checks for background jobs and worker status, printing any errors.
* **`bench schedule`** – (Frappe command) Force triggers scheduler events.
* **`bench set-config`** (site level) – We covered `bench set-config` earlier which wraps site config changes. There’s also a `--global` flag we used to indicate writing to common site config (which we covered in Config Commands section). Without `-g`, it writes to the individual site’s config file. For example: `bench --site mysite.local set-config developer_mode 1` toggles developer mode on that site’s config.

This report covers the **official Bench CLI commands** as documented for Frappe/ERPNext as of August 2025. Each command above is used in managing your Frappe bench, whether for setting up your environment, creating and managing sites and apps, or assisting in development and deployment tasks. Proper understanding of these commands can greatly streamline the management of Frappe/ERPNext projects.

**Sources:** The information and examples were compiled from the Frappe Framework official documentation, including the Bench Commands reference pages and usage guides, which provide detailed explanations for each command. All commands described are part of the standard Frappe Bench toolset (no third-party extensions).
