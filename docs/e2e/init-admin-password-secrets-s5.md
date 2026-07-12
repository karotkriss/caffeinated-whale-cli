# `init` secret hardening (M2) - real-instance E2E evidence

Worked evidence for routing `bench new-site`'s two secrets off the argv/echo and generating the admin password.
Run against a fully isolated throwaway instance (temporary `HOME`, unique `cwe2e-*` project, ports `18000-18005`/`19000-19005` away from the captain's real `ners` instance, dedicated volumes/network).
The captain's real `~/.cwcli` and real instances were never touched, and every throwaway container/volume/network/`HOME` was removed afterward.

Frappe resolved to `frappe/bench:v5.31.0` (version-16, `frappe 16.26.3`).
Both interactive (generated password) and non-interactive (supplied password) modes were exercised.

## What changed (recap)

Both secrets ride in the exec `environment=` dict and are referenced as unexpanded `$CWCLI_ADMIN_PASSWORD` / `$CWCLI_DB_ROOT_PASSWORD` in the command string, expanded by the in-container `bash -lc` at exec time (mirrors `restore.py`'s M5).
The admin password is generated when `--admin-password` is omitted (interactive only, printed once, gated on the site actually being created), used verbatim when supplied, and required (refuse) in a non-interactive run.

## Results

| # | Check | Interactive (generated) | Non-interactive (supplied) |
|---|-------|--------------------------|-----------------------------|
| 5 | Non-TTY + no `--admin-password` → clear error, exit 1, nothing created | n/a | ✅ |
| 4 | `-v` echo shows `$CWCLI_ADMIN_PASSWORD` / `$CWCLI_DB_ROOT_PASSWORD`, not the values | ✅ | ✅ |
| 2 | Generated password printed exactly once, gated | ✅ (1 occurrence in all output) | n/a (supplied ⇒ never printed) |
| 1 | Site created; the password authenticates as `Administrator` | ✅ (`check_password` ok; wrong pw → `AuthenticationError`) | ✅ (supplied pw → `Administrator`) |
| — | Secret value never leaks into cwcli output beyond the one gated print | ✅ (exactly 1) | ✅ (supplied value: 0 occurrences) |
| 3 | `docker top` shows no password in argv during new-site | ⚠️ see finding below | ⚠️ same |

### Point 5 - non-interactive refusal (instant, no Docker)

```
$ cwcli init cwe2e-refuse-test        # non-TTY, no --admin-password
Error: No --admin-password supplied and this is a non-interactive session.
Pass --admin-password to set the site's administrator password.
exit code: 1
```

No project dir was written.

### Point 4 - the verbose echo is safe (de-wrapped)

```
$ cd /workspace/frappe-bench && bench new-site --db-root-password "$CWCLI_DB_ROOT_PASSWORD" --admin-password "$CWCLI_ADMIN_PASSWORD" ...
```

The generated password value appeared **exactly once** in the entire init output (the final print) and **zero** times on any `$`-command echo line.
The supplied password (`Suppl!edPw#2`) appeared **zero** times anywhere in the non-interactive output.

### Points 1 & 2 - generated password works, printed once

```
Administrator password (generated): <redacted-24-char-token>
Shown once and not stored anywhere. To change it later, run
`bench --site gen1.localhost set-admin-password <new-password>`.

$ bench --site gen1.localhost execute frappe.auth.check_password \
    --kwargs '{"user":"Administrator","pwd":"<generated>"}'
Administrator                       # correct password authenticates
# wrong password → frappe.exceptions.AuthenticationError: Incorrect User or Password
```

Non-interactive supplied path: `bench --site gen1.localhost list-apps` returns `frappe 16.26.3`, and `check_password` with the supplied `Suppl!edPw#2` returns `Administrator`.

### Point 3 - honest finding: the leaf process argv still exposes the password

During `new-site`, `docker top` on the frappe container shows:

```
/workspace/frappe-bench/env/bin/python -m frappe.utils.bench_helper frappe new-site \
  --db-root-password 123 --admin-password <plaintext> --mariadb-user-host-login-scope=% gen1.localhost --verbose
```

`bench new-site` accepts the password **only** as a flag and reads no env/stdin password channel, so the in-container shell expands `$CWCLI_ADMIN_PASSWORD` into the child `frappe` python process's argv before exec - visible via `docker top` / `/proc/<pid>/cmdline` for the ~1-2 min the site is being created.

This residual exposure is **inherent to bench's flag-only interface** and is identical to what `restore.py`'s M5 pattern leaves.
The env-transport's real, verified win is that the secret is kept off cwcli's `-v` echo/logs and off the `bash -lc` wrapper argv / docker exec `Cmd` record.
Eliminating the leaf-process exposure would require a change to `bench` itself and is out of scope; do not "fix" it by reverting to inline argv (strictly worse) or `cmd.replace` masking (the pattern restore deliberately abandoned).

## Isolation confirmation

- Temporary `HOME` under the session scratchpad; fresh `~/.cwcli`.
- Ports `18000-18005` / `19000-19005`; the port-conflict guard correctly refused a second instance on the busy ports (pre-existing cwcli behavior).
- All `cwe2e-*` containers/volumes/network removed; temporary `HOME` deleted.
- `ners` (the captain's real instance) stayed `Up`; real `~/.cwcli` untouched.
