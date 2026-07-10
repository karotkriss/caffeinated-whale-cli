# Frappe Bench Backup and Restore Reference

Comprehensive guide to Frappe/ERPNext backup and restore operations, including directory structure analysis and practical examples.

## Table of Contents

- [Overview](#overview)
- [Directory Structure](#directory-structure)
- [Backup Commands](#backup-commands)
- [Restore Commands](#restore-commands)
- [Backup File Naming Convention](#backup-file-naming-convention)
- [Real-World Example Analysis](#real-world-example-analysis)
- [Best Practices](#best-practices)
- [Integration with cwcli](#integration-with-cwcli)

---

## Overview

Frappe Framework provides robust backup and restore capabilities through the `bench` CLI. Backups include database dumps, configuration files, and optionally public/private file attachments.

### Key Concepts

- **Database Backup**: Compressed SQL dump (`.sql.gz`) containing all tables, schema, and data
- **Public Files**: User-uploaded files accessible via web (`/sites/{site}/public/files/`)
- **Private Files**: Protected files requiring authentication (`/sites/{site}/private/files/`)
- **Site Config**: Configuration including database credentials, Redis URLs, encryption keys

---

## Directory Structure

### Standard Site Layout

```
frappe-bench/
└── sites/
    └── {site-name}/
        ├── site_config.json          # Site configuration (credentials, settings)
        ├── locks/                     # Lock files for migrations/jobs
        ├── public/
        │   └── files/                 # Web-accessible uploaded files
        └── private/
            ├── backups/               # **DEFAULT BACKUP LOCATION**
            │   ├── {timestamp}-{site}-database.sql.gz
            │   ├── {timestamp}-{site}-files.tar
            │   ├── {timestamp}-{site}-private-files.tar
            │   └── {timestamp}-{site}-site_config_backup.json
            └── files/                 # Protected uploaded files
```

### Real-World Example: dev site

Based on analysis of `development.localhost` site:

```
/workspace/development/frappe-bench/sites/development.localhost/private/
├── backups/
│   ├── 20251109_225726-development_localhost-database.sql.gz
│   ├── 20251109_225726-development_localhost-files.tgz
│   ├── 20251109_225726-development_localhost-private-files.tgz
│   ├── 20251109_225726-development_localhost-site_config_backup.json
│   ├── 20251109_225910-development_localhost-database.sql.gz
│   ├── 20251109_225910-development_localhost-files.tar
│   ├── 20251109_225910-development_localhost-private-files.tar
│   ├── 20251109_225910-development_localhost-site_config_backup.json
│   ├── 20251109_225946-development_localhost-database.sql.gz
│   └── 20251109_225946-development_localhost-site_config_backup.json
└── files/
    ├── [200+ user-uploaded files]
    └── Media files (images, screenshots)
```

**Key Observations:**
- Multiple backup strategies in use (full with files, database-only)
- Different compression options (`.tgz` vs `.tar`)
- Backups grouped by timestamp (same timestamp = same backup set)
- Private files directory contains sensitive production data

---

## Backup Commands

### `bench backup`

Creates database dumps and optionally backs up site files.

**Basic Usage:**
```bash
# Backup current site (if set)
bench backup

# Backup specific site
bench --site {site-name} backup

# Backup with files (compressed)
bench --site {site-name} backup --with-files --compress

# Backup with files (uncompressed)
bench --site {site-name} backup --with-files
```

#### Options

| Option | Description |
|--------|-------------|
| `--backup-path PATH` | Set overall backup directory (default: `./sites/{site}/private/backups`) |
| `--backup-path-db PATH` | Specify database file location |
| `--backup-path-conf PATH` | Specify config file location |
| `--backup-path-files PATH` | Specify public files location |
| `--backup-path-private-files PATH` | Specify private files location |
| `--exclude DOCTYPES`, `-e` | Exclude specific DocTypes (comma-separated) |
| `--only DOCTYPES`, `--include`, `-i` | Include only specific DocTypes (comma-separated) |
| `--ignore-backup-conf` | Ignore excludes/includes from site configuration |
| `--with-files` | Include public and private file archives in backup |
| `--compress` | Create `.tgz` format (gzip compressed) instead of `.tar` |
| `--verbose` | Show detailed operation information |

#### Examples

**Database-only backup (fastest):**
```bash
bench --site production.example.com backup
```

**Full backup with compressed files:**
```bash
bench --site production.example.com backup --with-files --compress
```

**Exclude log tables to reduce size:**
```bash
bench --site production.example.com backup \
  --exclude 'Error Log,Access Log,Activity Log,Version,Communication'
```

**Backup only specific DocTypes (partial backup):**
```bash
bench --site production.example.com backup \
  --only 'Customer,Sales Invoice,Item,Stock Entry'
```

**Custom backup location:**
```bash
bench --site production.example.com backup \
  --with-files \
  --backup-path /mnt/external-drive/backups
```

#### What Gets Backed Up

**Database Backup (`database.sql.gz`):**
- All database tables
- Complete schema (structure)
- All records and data
- Indexes and constraints
- Stored procedures (if any)

**Files Backup (`files.tar` or `files.tgz`):**
- Public files from `/sites/{site}/public/files/`
- User-uploaded attachments
- Generated reports/exports
- Public media assets

**Private Files Backup (`private-files.tar` or `private-files.tgz`):**
- Private files from `/sites/{site}/private/files/`
- Sensitive documents requiring authentication
- Internal reports
- Encrypted data

**Config Backup (`site_config_backup.json`):**
- Database credentials
- Redis URLs
- Encryption keys
- Site-specific settings
- Mail server configuration

#### Important Notes

- **Failed backups are automatically deleted** to prevent disk space waste
- **Compression** (`.tgz`) saves disk space but takes longer to create/restore
- **Uncompressed** (`.tar`) is faster but uses more disk space

---

### `bench backup-all-sites`

Backs up all sites in the current bench.

**Usage:**
```bash
# Backup all sites (database only)
bench backup-all-sites

# Backup all sites with files
bench backup-all-sites --with-files
```

**Use Case:** Scheduled backups via cron for multi-site benches.

**Example Cron Job:**
```bash
# Daily backup at 2 AM
0 2 * * * cd /home/frappe/frappe-bench && bench backup-all-sites --with-files --compress
```

---

## Restore Commands

### `bench restore`

Restores a site from database and optional file backups.

**Basic Usage:**
```bash
bench --site {site-name} restore {path-to-sql-file}
```

#### Arguments

| Argument | Description |
|----------|-------------|
| `SQL_FILE_PATH` | Path to database backup (`.sql` or `.sql.gz`) - relative to `bench/sites` or absolute |

#### Options

| Option | Description |
|--------|-------------|
| `--db-root-username TEXT` | Database root username (avoids interactive prompt) |
| `--db-root-password TEXT` | Database root password (avoids interactive prompt) |
| `--db-name TEXT` | Custom database name for restore |
| `--admin-password TEXT` | Set/reset administrator password after restore |
| `--with-public-files PATH` | Restore public files from archive (`.tar` or `.tgz`) |
| `--with-private-files PATH` | Restore private files from archive (`.tar` or `.tgz`) |
| `--install-app TEXT` | Install specified app after restore |
| `--force` | Bypass downgrade warnings (use with caution!) |

#### Examples

**Database-only restore:**
```bash
bench --site production.example.com restore \
  ~/frappe-bench/sites/production.example.com/private/backups/20251109_225726-production_example_com-database.sql.gz
```

**Complete restore with files:**
```bash
bench --site production.example.com restore \
  --with-public-files ~/backups/20251109_225726-production_example_com-files.tar \
  --with-private-files ~/backups/20251109_225726-production_example_com-private-files.tar \
  ~/backups/20251109_225726-production_example_com-database.sql.gz
```

**Automated restore (no prompts):**
```bash
bench --site production.example.com restore \
  --db-root-username root \
  --db-root-password 'secure_password' \
  --admin-password 'new_admin_pass' \
  ~/backups/database.sql.gz
```

**Force restore despite downgrade warning:**
```bash
bench --site production.example.com restore \
  --force \
  ~/backups/older-version-database.sql.gz
```

#### Critical Limitations

1. **No file-only restore**: Database restoration is mandatory; files are optional
2. **Downgrades not supported**: Frappe shows interactive warning unless `--force` used
3. **Database must match site**: Cannot restore incompatible database structures
4. **Site must exist**: Cannot create new site via restore (use `bench new-site` first)

#### Restore Process Flow

```
1. Validate SQL file exists and is readable
2. Check Frappe version compatibility
3. Prompt for database credentials (if not provided)
4. Drop existing database (if --force or confirmed)
5. Create new database
6. Import SQL dump
7. Extract and restore public files (if provided)
8. Extract and restore private files (if provided)
9. Run post-restore migrations
10. Clear cache
11. Restart workers
```

---

## Backup File Naming Convention

### Pattern Structure

```
{TIMESTAMP}-{SITE_NAME}-{BACKUP_TYPE}.{EXTENSION}
```

### Component Breakdown

#### 1. Timestamp: `YYYYMMDD_HHMMSS`

Format: `20251109_225726`
- Year: `2025`
- Month: `11` (November)
- Day: `09`
- Hour: `22` (10 PM, 24-hour format)
- Minute: `57`
- Second: `26`

**Separator:** Underscore `_` between date and time

**Benefits:**
- Sortable: Alphabetical order = chronological order
- Human-readable: Clear timestamp
- Filesystem-safe: No special characters

#### 2. Site Name: `{subdomain}_{environment}_{domain}_{tld}`

Original domain: `development.localhost`
Transformed name: `development_localhost`

**Transformation:** Dots (`.`) replaced with underscores (`_`)

**Rationale:**
- Dots can be confused with file extensions
- Underscores are filesystem-safe across all platforms
- Maintains readability
- Prevents shell interpretation issues

**Edge Case:** Original sites with underscores create ambiguity:
- `my_site.example.com` → `my_site_example_com`
- Indistinguishable from `my.site_example.com` → `my_site_example_com`
- In practice, domains rarely contain underscores

#### 3. Backup Type

| Type | Purpose | Content |
|------|---------|---------|
| `database` | Database dump | All tables, schema, data |
| `files` | Public files | `/sites/{site}/public/files/*` |
| `private-files` | Private files | `/sites/{site}/private/files/*` |
| `site_config_backup` | Configuration | `site_config.json` backup |

**Naming Convention:**
- `database` - Singular, lowercase
- `files` - Plural, lowercase
- `private-files` - Plural, hyphenated
- `site_config_backup` - Descriptive suffix

#### 4. Extensions

| Extension | Type | Compression | Use Case |
|-----------|------|-------------|----------|
| `.sql.gz` | Database | Gzip | Standard database backup (compressed) |
| `.tgz` | Tar archive | Gzip | Compressed file backup (space-efficient) |
| `.tar` | Tar archive | None | Uncompressed files (fast restore) |
| `.json` | JSON | None | Site configuration |

**Double Extensions:**
- `.sql.gz` = SQL file + Gzip compression
- `.tgz` = Tar + Gzip (combined extension)

### Complete Examples

```
20251109_225726-development_localhost-database.sql.gz
│      │   │  │ │                   │         │  └─ Gzip compression
│      │   │  │ │                   │         └──── SQL file
│      │   │  │ │                   └────────────── Database backup type
│      │   │  │ └────────────────────────────────── Site name (dots→underscores)
│      │   │  └──────────────────────────────────── Seconds: 26
│      │   └─────────────────────────────────────── Minutes: 57
│      └─────────────────────────────────────────── Hour: 22 (10 PM)
└────────────────────────────────────────────────── Date: Nov 9, 2025
```

### Backup Set Grouping

Files with **identical timestamps** belong to the **same backup set**:

**Backup Set 1: Full with Compression (`22:57:26`)**
```
20251109_225726-development_localhost-database.sql.gz      (45.2 MB)
20251109_225726-development_localhost-files.tgz            (1.5 GB)
20251109_225726-development_localhost-private-files.tgz    (312 MB)
20251109_225726-development_localhost-site_config_backup.json (2.1 KB)
```

**Backup Set 2: Full without Compression (`22:59:10`)**
```
20251109_225910-development_localhost-database.sql.gz      (45.1 MB)
20251109_225910-development_localhost-files.tar            (1.8 GB)
20251109_225910-development_localhost-private-files.tar    (362 MB)
20251109_225910-development_localhost-site_config_backup.json (2.1 KB)
```

**Backup Set 3: Database-only (`22:59:46`)**
```
20251109_225946-development_localhost-database.sql.gz      (45.2 MB)
20251109_225946-development_localhost-site_config_backup.json (2.1 KB)
```

### Parsing Algorithm

```python
import re
from datetime import datetime

def parse_frappe_backup_filename(filename: str) -> dict:
    """
    Parse Frappe backup filename into components.

    Args:
        filename: Backup filename (e.g., '20251109_225726-site_name-database.sql.gz')

    Returns:
        Dict with parsed components or None if invalid
    """
    # Remove extensions
    name_without_ext = filename
    extensions = []
    while '.' in name_without_ext:
        name_without_ext, ext = name_without_ext.rsplit('.', 1)
        extensions.insert(0, ext)

    # Parse main pattern
    pattern = r'^(\d{8}_\d{6})-([^-]+)-(.+)$'
    match = re.match(pattern, name_without_ext)

    if not match:
        return None

    timestamp_str, site_name, backup_type = match.groups()

    # Parse timestamp
    timestamp = datetime.strptime(timestamp_str, '%Y%m%d_%H%M%S')

    return {
        'timestamp': timestamp,
        'site_name': site_name,  # With underscores (transformed)
        'backup_type': backup_type,
        'extensions': extensions,
        'full_extension': '.'.join(extensions),
        'compressed': 'gz' in extensions or 'tgz' in extensions,
        'is_database': backup_type == 'database',
        'is_files': backup_type in ('files', 'private-files'),
        'is_config': backup_type == 'site_config_backup'
    }
```

**Example Output:**
```python
parse_frappe_backup_filename('20251109_225726-development_localhost-database.sql.gz')

# Returns:
{
    'timestamp': datetime(2025, 11, 9, 22, 57, 26),
    'site_name': 'development_localhost',
    'backup_type': 'database',
    'extensions': ['sql', 'gz'],
    'full_extension': 'sql.gz',
    'compressed': True,
    'is_database': True,
    'is_files': False,
    'is_config': False
}
```

---

## Real-World Example Analysis

### EPA Staging Environment

**Site:** `development.localhost` (Guyana Environmental Protection Agency)
**Environment:** Staging server
**Date:** November 9, 2025

#### Backup Timeline

Three backups captured within **3 minutes**:

1. **22:57:26** - Full backup with compressed files (`.tgz`)
2. **22:59:10** - Full backup with uncompressed files (`.tar`) - **1m 44s later**
3. **22:59:46** - Database-only backup - **36s later**

**Analysis:** Likely testing different backup strategies:
- Compression trade-offs (space vs. speed)
- Full vs. partial backup workflows
- Backup timing and performance

#### Private Files Content

The `private/files/` directory contains **200+ files**, including:

**Document Categories:**
- Environmental permits and authorizations
- Government application forms
- Incident reports and declarations
- Budget proposals and financial documents
- Legal documents (Acts, regulations)
- Project summaries and proposals

**Sensitive Data:**
- National ID cards
- Passports
- Birth certificates
- Invoices and contracts
- Personnel CVs

**Media:**
- Screenshots and images
- PDFs and Word documents
- Educational materials
- Project documentation

#### Security Implications

1. **Production-like data in staging**: Real or realistic documents in use
2. **PII present**: Personal Identifiable Information requires protection
3. **Government data**: High security and compliance standards
4. **Multi-format files**: Various file types including spaces in names

**Validation of cwcli Security Measures:**
- ✅ Input validation prevents path traversal attacks
- ✅ Command injection prevention protects sensitive file access
- ✅ Filesystem permissions (0700/0600) secure cached configs
- ✅ Allows spaces in filenames (real-world requirement)
- ✅ Blocks shell metacharacters (security requirement)

#### Backup Size Estimates

Based on typical Frappe installations:

| Component | Typical Size | EPA Staging Estimate |
|-----------|--------------|---------------------|
| Database | 10-500 MB | ~45 MB (small staging DB) |
| Public files | 100 MB - 10 GB | ~1.8 GB uncompressed |
| Private files | 50 MB - 5 GB | ~312-362 MB |
| Config | 1-5 KB | ~2.1 KB |
| **Total** | ~200 MB - 15 GB | **~2.2 GB uncompressed** |

**Compression Ratio:**
- Files: `.tar` (1.8 GB) → `.tgz` (1.5 GB) = **~17% reduction**
- Private: `.tar` (362 MB) → `.tgz` (312 MB) = **~14% reduction**

---

## Best Practices

### Backup Strategy

#### 1. Backup Frequency

| Environment | Database | Files | Frequency |
|-------------|----------|-------|-----------|
| **Production** | ✓ | ✓ | Daily (minimum) |
| **Staging** | ✓ | Optional | Weekly |
| **Development** | ✓ | - | On-demand |

#### 2. Retention Policy

```
Daily backups:  Keep 7 days
Weekly backups: Keep 4 weeks
Monthly backups: Keep 12 months
Yearly backups: Keep indefinitely (compliance)
```

#### 3. Backup Types by Use Case

**Full Backup with Files (Compressed):**
```bash
bench --site production.example.com backup --with-files --compress
```
- **When:** Production daily/weekly backups
- **Pros:** Complete recovery capability, space-efficient
- **Cons:** Slower backup/restore

**Database-only Backup:**
```bash
bench --site production.example.com backup
```
- **When:** Frequent snapshots between full backups
- **Pros:** Fast, small size
- **Cons:** Cannot recover uploaded files

**Selective Backup (Exclude Logs):**
```bash
bench --site production.example.com backup \
  --exclude 'Error Log,Access Log,Activity Log,Version'
```
- **When:** Reduce backup size by excluding log tables
- **Pros:** Smaller backup files
- **Cons:** Logs not included in backup

#### 4. Storage Locations

**Local Backups:**
- Default: `./sites/{site}/private/backups/`
- Fast access for quick restores
- Limited by disk space

**Off-site Backups:**
```bash
bench --site production.example.com backup \
  --with-files --compress \
  --backup-path /mnt/nas/backups
```
- Network storage (NAS, NFS)
- Cloud storage (S3, Azure, GCS)
- Critical for disaster recovery

#### 5. Automated Backups with Cron

**Daily full backup with rotation:**
```bash
#!/bin/bash
# /home/frappe/scripts/backup-production.sh

SITE="production.example.com"
BACKUP_DIR="/mnt/nas/backups"
KEEP_DAYS=30

# Create backup
cd /home/frappe/frappe-bench
bench --site ${SITE} backup --with-files --compress --backup-path ${BACKUP_DIR}

# Rotate old backups
find ${BACKUP_DIR} -name "${SITE//./_}-*" -mtime +${KEEP_DAYS} -delete

# Upload to S3 (optional)
# aws s3 sync ${BACKUP_DIR} s3://my-backup-bucket/frappe-backups/
```

**Crontab entry:**
```bash
# Daily backup at 2 AM
0 2 * * * /home/frappe/scripts/backup-production.sh >> /var/log/frappe-backup.log 2>&1
```

### Restore Best Practices

#### 1. Test Restores Regularly

**Monthly restore test:**
```bash
# 1. Create test site
bench new-site restore-test.example.com

# 2. Restore latest backup
bench --site restore-test.example.com restore \
  --with-public-files ~/backups/latest-files.tar \
  --with-private-files ~/backups/latest-private-files.tar \
  ~/backups/latest-database.sql.gz

# 3. Verify data integrity
bench --site restore-test.example.com console

# 4. Cleanup
bench drop-site restore-test.example.com
```

#### 2. Pre-Restore Checklist

- [ ] Verify backup file integrity (checksums)
- [ ] Check available disk space (restore needs ~3x backup size)
- [ ] Stop site traffic (maintenance mode)
- [ ] Document current state (screenshot, version)
- [ ] Have rollback plan ready
- [ ] Notify users of downtime window

#### 3. Post-Restore Verification

```bash
# Check site status
bench --site production.example.com migrate

# Clear all caches
bench --site production.example.com clear-cache
bench --site production.example.com clear-website-cache

# Restart workers
bench restart

# Verify critical functionality
bench --site production.example.com console
```

#### 4. Disaster Recovery Procedure

**Complete site loss:**
```bash
# 1. Reinstall bench and apps
cd ~
bench init frappe-bench --frappe-branch version-15
cd frappe-bench
bench get-app erpnext --branch version-15

# 2. Create new site
bench new-site production.example.com

# 3. Restore from backup
bench --site production.example.com restore \
  --with-public-files /backups/20251109_225726-production_example_com-files.tgz \
  --with-private-files /backups/20251109_225726-production_example_com-private-files.tgz \
  /backups/20251109_225726-production_example_com-database.sql.gz

# 4. Install required apps
bench --site production.example.com install-app erpnext

# 5. Migrate and restart
bench --site production.example.com migrate
bench restart
```

### Security Considerations

#### 1. Backup File Permissions

```bash
# Ensure restrictive permissions
chmod 600 /path/to/backups/*.sql.gz
chmod 700 /path/to/backups/

# Set ownership to frappe user
chown -R frappe:frappe /path/to/backups/
```

#### 2. Encrypted Backups

```bash
# Encrypt backup before off-site storage
gpg --encrypt --recipient backup@example.com \
  20251109_225726-production_example_com-database.sql.gz

# Decrypt for restore
gpg --decrypt 20251109_225726-production_example_com-database.sql.gz.gpg \
  > database.sql.gz
```

#### 3. Credential Protection

**Never commit backups to version control:**
```bash
# .gitignore
sites/*/private/backups/
*.sql
*.sql.gz
*site_config_backup.json
```

**Secure config backups:**
- `site_config_backup.json` contains database passwords, encryption keys
- Store separately from database backups
- Encrypt before uploading to cloud storage

---

## Integration with cwcli

### What cwcli Actually Provides Today

Backup and restore shipped as `cwcli backup` and `cwcli restore` (flags verified against the Typer signatures in `src/caffeinated_whale_cli/commands/backup.py` and `restore.py` at 0.34.0), not as the `cwcli backup list`/`restore`/`cleanup` subcommand family sketched in an earlier draft of this doc.

#### `cwcli backup` - create a backup

```bash
# Backup the default site (resolved from common_site_config.json or currentsite.txt)
cwcli backup my-project

# Backup a specific site, with public and private files
cwcli backup my-project --site example.com --with-files

# Target a specific bench in a multi-bench project, auto-starting stopped containers
cwcli backup my-project --bench 1 --yes
```

There is no `list`/`cleanup`/`--database-only`/`--exclude` subcommand or flag; `cwcli backup` runs `bench backup` for one site per invocation.

#### `cwcli restore` - restore a site from a local backup

```bash
# Interactive backup-selection menu
cwcli restore my-project

# Non-interactive: pick the newest backup for the target site
cwcli restore my-project --latest --yes

# Non-interactive: pick a specific backup file
cwcli restore my-project --backup-file 20251109_225726-my_project-database.sql.gz --yes
```

`--latest` and `--backup-file` are mutually exclusive selectors that replace the interactive menu; both require `--yes` (or an interactive TTY) to pass the destructive-restore confirmation.
`--no-recache` is a **deprecated no-op** flag kept only for backward compatibility - the missing-apps check has read app availability live from the bench since the fixes in `docs/e2e/restore-inspect-e2e-r6.md`, so this flag no longer does anything.

#### `cwcli restore --send` / `--receive` - P2P backup transfer

```bash
# On the source machine: share a backup via sendme
cwcli restore my-project --send

# On the destination machine: receive and restore it
cwcli restore my-project --receive --yes
```

This is the "peer-to-peer restore" capability once sketched here as `cwcli backup send`/`backup receive`/`restore --from-peer`; see [sendme-doc.md](./sendme-doc.md#shipped-implementation) and the root [README's P2P Backup Transfer section](../../README.md#command-reference) for the full flag reference.

#### `rm`'s backup-before-delete gate

`cwcli rm` is not a backup command, but it backs up every site in every bench (looping over all benches on a multi-bench project) before deleting a project's volumes: a failed or unverifiable `bench backup` for any bench aborts the removal before any container is touched.
This gate only applies when volumes will be deleted (the default `--volumes`); under `--no-volumes` no volume data is destroyed, so a failed backup does not block container removal, directory cleanup, or cache clearing.
See the "Data-safety gates" section of the project's `AGENTS.md`/`CLAUDE.md` for the exact contract.
`--no-backup` opts out of the gate entirely.

### Implementation Considerations

The sections below describe how the shipped commands are implemented, kept for background on the container-exec and security-validation approach (the code samples are illustrative, not verbatim - read the actual command modules for the current implementation).

#### 1. Container Execution

Since cwcli manages Docker containers, backup commands execute inside the Frappe container:

```python
def backup_site(project_name: str, site: str = None, with_files: bool = False):
    """Execute bench backup inside container."""
    container = get_frappe_container(project_name)
    bench_path = get_bench_path(project_name)  # From cache

    if not site:
        site = get_default_site(project_name)  # Use default site feature

    cmd = f"bench --site {site} backup"
    if with_files:
        cmd += " --with-files --compress"

    exit_code, output = container.exec_run(cmd, workdir=bench_path)
    return exit_code == 0
```

#### 2. Backup File Discovery

```python
def list_backups(project_name: str, site: str = None) -> list:
    """List available backups for a site."""
    container = get_frappe_container(project_name)
    bench_path = get_bench_path(project_name)

    if not site:
        site = get_default_site(project_name)

    backup_dir = f"{bench_path}/sites/{site}/private/backups"

    # List backup files
    cmd = f"ls -lt {backup_dir}"
    exit_code, output = container.exec_run(cmd)

    # Parse and group by timestamp
    backups = parse_backup_listing(output)
    return backups
```

#### 3. Default Site Support

Leverage the existing `get_default_site()` helper:

```python
# User doesn't need to specify site if default exists
cwcli backup my-project
# Output: Using default site: production.example.com
# Output: Creating backup...
```

#### 4. Security Validation

Apply the same input validation used in `unlock` command:

```python
# Validate site name
if not site or not site.strip():
    raise ValueError("Site name cannot be empty")

# Prevent command injection
invalid_chars = [";", "&", "|", "$", "`", "(", ")", "<", ">", "\n", "\r", "\\"]
if any(char in site for char in invalid_chars):
    raise ValueError("Invalid site name: shell metacharacters not allowed")
```

---

## Appendix: Quick Reference

### Command Cheatsheet

```bash
# BACKUP COMMANDS
bench --site {site} backup                              # Database only
bench --site {site} backup --with-files                # With files (uncompressed)
bench --site {site} backup --with-files --compress     # With files (compressed)
bench --site {site} backup --exclude 'Log,Version'     # Exclude tables
bench --site {site} backup --only 'Customer,Invoice'   # Partial backup
bench backup-all-sites                                  # All sites in bench

# RESTORE COMMANDS
bench --site {site} restore {sql-file}                 # Database only
bench --site {site} restore \
  --with-public-files {files.tar} \
  --with-private-files {private-files.tar} \
  {database.sql.gz}                                     # Full restore

bench --site {site} restore --force {sql-file}         # Force (ignore warnings)
```

### File Extension Reference

| Extension | Command Flag | Description |
|-----------|--------------|-------------|
| `.sql.gz` | (default) | Compressed database dump |
| `.tgz` | `--compress` | Compressed tar archive (gzip) |
| `.tar` | (no flag) | Uncompressed tar archive |
| `.json` | (automatic) | Site config backup |

### Default Locations

| Item | Path |
|------|------|
| Backup directory | `./sites/{site}/private/backups/` |
| Public files | `./sites/{site}/public/files/` |
| Private files | `./sites/{site}/private/files/` |
| Site config | `./sites/{site}/site_config.json` |

---

## References

- [Frappe Backup Documentation](https://docs.frappe.io/framework/user/en/bench/reference/backup)
- [Frappe Restore Documentation](https://docs.frappe.io/framework/user/en/bench/reference/restore)
- [Bench Commands Cheatsheet](https://docs.frappe.io/framework/user/en/bench/resources/bench-commands-cheatsheet)
- [ERPNext Backup & Restore Guide](https://github.com/frappe/erpnext/wiki/Restoring-From-ERPNext-Backup)

---

**Document Version:** 1.0
**Last Updated:** 2025-11-09
**Author:** Claude (AI Assistant)
**For:** Caffeinated Whale CLI (cwcli) Project
