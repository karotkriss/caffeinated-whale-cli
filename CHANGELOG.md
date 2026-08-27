# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`cwcli inspect` and `cwcli axi inspect` now accept `--bench <index|label>`**, narrowing the report to one bench on a multi-bench project - the same selector `status`, `logs`, `restart`, `stop`, and every `apps` verb already take.
Every tier still discovers, refreshes, and caches every bench regardless of the selector; only the rendered/emitted report is narrowed.
An unknown selector fails the same way every other bench-scoped verb does.

## [3.0.0] - 2026-08-22

**This is a major release because `cwcli apps install` and `cwcli apps uninstall` no longer take app names as a positional argument:** they now ride a repeatable `--app`/`-a` option, so any existing invocation of the old shape needs updating.
Opening a Cursor dev container no longer aborts on a transient marketplace error when the Dev Containers extension is already installed, and the passive update notice is legible again.

### Changed

- **BREAKING: `cwcli apps install` and `cwcli apps uninstall` take app names on `--app`, not as a positional** - both used to accept a second positional argument holding the app list, which broke the human-tier convention of at most one positional after the subcommand (the project name) with everything else as an option, the shape `cwcli open --app` already followed.
App names now ride a repeatable `--app`/`-a` option, matching `open`'s flag name and style: `cwcli apps install myproj --app erpnext --app hrms`.
**Anything invoking the old shape needs updating.**
At least one `--app` is required, so an omitted flag is a clear usage error rather than a silently accepted old-shape call.
This is a hard cut with no deprecated positional fallback, because nothing shipped here invoked the old shape non-interactively; `cwcli axi apps install` is a separate verb and is unchanged.

### Fixed

- **`cwcli open` no longer aborts a Cursor dev-container open on a transient extension-install error when the extension is already installed** - Cursor aliases the Microsoft Dev Containers extension id (`ms-vscode-remote.remote-containers`) to its own publisher id (`anysphere.remote-containers`), so listing Cursor's extensions never reported the id cwcli looked for.
cwcli therefore believed the extension was always missing on Cursor, ran a needless install on every open, and aborted the open outright when Cursor's marketplace answered that install with a transient 5xx - even though the extension was already installed locally.
Both ids are now recognised, an install that reports the extension is already present counts as success, and a failed install rechecks locally before giving up.
A genuinely missing extension still refuses and surfaces the real error.
- **The passive update notice is no longer dim** - the once-a-day "a newer cwcli is available" hint rendered as dim text and was routinely missed.
Its body is now yellow, matching the warning styling used elsewhere, with the upgrade command still in cyan.
The notice text and when it appears are unchanged.

### Other Changes

- **The Homebrew tap formula bump now merges itself** - a release already opened a formula-bump PR against `karotkriss/homebrew-cwcli`; that PR now merges automatically once the tap's own CI passes, instead of waiting on a manual click.
A failing or still-running tap check leaves the PR open and untouched, and this step can never affect the PyPI publish or the GitHub release.

## [2.6.0] - 2026-08-08

`cwcli` now offers an opt-in persistent credential bridge so private-repo git authentication also works inside `cwcli open` and interactive shells, not just cwcli's own operations. `cwcli init --site` accepts any hostname, FQDN, or IPv4 address instead of requiring a `.localhost` name, and `cwcli backup`/`cwcli restore` no longer dead-end on a cold cache.

### Added
- **Persistent credential bridge (`cwcli config cred-bridge`)**: An opt-in, detached host daemon that extends cwcli's private-repo git authentication (host `gh`/`glab`, no token ever entering the container) to interactive git - inside an editor or shell opened via `cwcli open`, or a plain `docker exec` - not just cwcli's own install/update/init operations. Enable with `cwcli config cred-bridge enable`; a stopped or never-started daemon degrades silently to git's normal prompt, so opting in is fully reversible. Includes an opt-in boot-persistence flag (`--startup`) so the bridge survives a reboot, a host allowlist (`[cred_bridge] allowed_hosts`, defaulting to `github.com` and `gitlab.com`) enforced before any credential request is served, and permissions hardening on cwcli's project directory. `cwcli run` now wraps both of its exec paths in the same bridge. There is deliberately no agent-facing verb for this: standing up a persistent host credential channel stays a decision a person makes.
- **`cwcli init --site` accepts any hostname/FQDN or IPv4 address**: `.localhost` is now a suggestion for local development rather than a requirement, so a site can be named after a real domain or IP address. `.localhost` names still resolve without any DNS setup and remain the default; a non-`.localhost` name prints a one-time note that it needs to resolve to your machine to be reachable from a browser.

### Fixed
- **`cwcli backup` and `cwcli restore` no longer dead-end on a cold cache**: without a cached bench location, both used to guess a hardcoded default path instead of discovering the real one, which failed outright on any bench not sitting at that path (most visibly `cwcli axi backup`, which has no fallback prologue of its own). They now run the same inspect-based cache-population `cwcli open` already relied on before resolving the bench.

### Other Changes
- **Standalone Windows build (`cwcli.exe`)**: Tagged releases now also publish a Nuitka-compiled standalone executable for Windows, plus a `python -m caffeinated_whale_cli` entry point for locked-down hosts where Application Control or SmartScreen blocks the unsigned launcher shim. Code-signing through SignPath is wired in but stays inactive until signing credentials are configured, so this release still ships unsigned.

## [2.5.0] - 2026-08-06

`cwcli apps update` can now recover from a per-app git conflict instead of failing outright, the git credential bridge works on Windows and macOS, and cwcli's own releases now keep the Homebrew tap formula in sync automatically.

### Added
- **`cwcli apps update --force`**: An opt-in flag that recovers from a per-app git conflict during an update by hard-resetting that app to its remote tracking branch (`git fetch` + `git reset --hard @{u}`), discarding local changes so the update completes instead of failing. Without `--force`, a conflict still fails and discards nothing, exactly as before. Available on both `cwcli apps update` and `cwcli axi apps update`.

### Fixed
- **The git credential bridge now works on Windows and macOS**: `cwcli init --frappe-url <private fork>` used to crash on Windows with `AttributeError: module 'socket' has no attribute 'AF_UNIX'` because the bridge's only transport was a host unix-domain socket, which Windows Python doesn't support and which Docker Desktop bind mounts can't carry into the container on either Windows or macOS. The bridge now uses that proven socket transport on native Linux and a token-gated loopback-TCP transport on Docker Desktop hosts (Windows and macOS).

### Other Changes
- **Releases now auto-bump the Homebrew tap formula**: a cwcli release now updates `Formula/cwcli.rb` in `karotkriss/homebrew-cwcli` with the newly published PyPI sdist URL and checksum, via a PR that the tap's own CI validates before it lands. A failure in this step can never affect the PyPI publish or GitHub release.

## [2.4.0] - 2026-08-05

`cwcli init` can now build from a custom Frappe fork instead of the default `frappe/frappe`, and a private fork authenticates automatically.

### Added
- **`cwcli init --frappe-url <url>`**: Builds the bench from a custom Frappe repository instead of the default `frappe/frappe`. Pair it with the existing `--frappe-branch` to check out a specific branch on that fork. Omitting `--frappe-url` builds the default repo exactly as before.
- **Private `--frappe-url` forks authenticate through the credential bridge**: A private fork passed to `--frappe-url` now authenticates automatically through cwcli's git credential bridge, the same way private app fetches already do, so `bench init` can clone it with no extra setup on your part.

## [2.3.1] - 2026-08-05

`cwcli init` could create an instance with no ports published to the host after an upstream change to the Frappe Docker template, leaving it running but unreachable. It now detects and fixes that itself.

### Fixed
- **`cwcli init` no longer creates unreachable instances when the upstream Docker template drops its ports block**: An upstream change to the Frappe Docker template `init` relies on could omit the `ports:` block, so a new instance started successfully but published no ports to the host and could not be reached from a browser or `cwcli open`.
`init` now detects missing ports and writes them itself, retries a transient port conflict left by an instance you just removed, and warns loudly if an instance ever ends up with no reachable host port instead of leaving it silently unreachable.

## [2.3.0] - 2026-08-05

`cwcli apps install` can now be asked to *ensure* an app is installed rather than to install it fresh, and it no longer fails on a bench that already carries the app.
The Docker-daemon-unreachable error on the `axi` surface now tells an agent what to do about it.

### Added
- **`cwcli apps install --if-not-present` (and `cwcli axi apps install --if-not-present`)**: An opt-in idempotent "ensure installed" mode reports an app already installed on a target site as a skipped step and exits 0 without re-running its install hooks, while an app the site lacks still installs normally and a genuine failure still exits non-zero.
This replaces workarounds like `apps install ... || true` that swallowed every failure.
Default behaviour is unchanged: without the flag, an already-installed app is still refused.
This is not a `--force`; there is still no way to reinstall over an app the site already has.

### Fixed
- **`cwcli apps install` no longer fails when the app is already present on the bench**: Installing an app whose directory already existed under the bench's `apps/` (as on a pre-warmed base image) failed the whole command at the fetch step.
The fetch is now skipped when the app is already on the bench, and installation proceeds to the site.
This is distinct from the per-site already-installed guard, whose default refusal remains unchanged.
- **The `axi` Docker-unreachable error now carries a next-step hint**: A stopped Docker daemon previously produced an `axi` error with no guidance line.
It now points at the same fix `cwcli doctor` gives.

## [2.2.0] - 2026-08-01

`cwcli init` no longer spends minutes aligning the container user to your host UID/GID; that step now takes seconds. A new `cwcli doctor` command checks whether your machine can run cwcli at all, and several bench-discovery and error-reporting gaps are closed.

### Added
- **`cwcli doctor` / `cwcli axi doctor`** - A read-only preflight check of whether cwcli can operate on this machine at all: Docker and Docker Compose, cwcli's own config and cache directories, free disk space, `sendme`/`gh`/`glab` availability, and port ranges colliding across your instances. A warning exits 0 so it stays safe to chain in a script; a failure exits non-zero

### Changed
- **`cwcli init`'s container user alignment dropped from minutes to seconds** - Aligning the container's `frappe` user to your host UID/GID (needed so `cwcli rm` can clean up files it creates) used to run `usermod`, which recursively copies the entire container home directory into Docker's writable layer the first time anything in it changes. That copy is what took minutes. The alignment now edits `/etc/passwd`/`/etc/group` directly and limits ownership repair to the paths `init` actually writes to, leaving baked toolchain files (pyenv, nvm) untouched
- **`cwcli init` and `cwcli scale` check for the Docker Compose plugin up front** - Both used to assume `docker compose` was available and only found out otherwise mid-run, after already creating a project directory or rewriting a compose file. A missing Compose plugin is now reported before anything is touched, with a plain install hint
- **`cwcli inspect` discovers benches created directly under the container workspace** - A bench created by hand inside the container (rather than through `cwcli init`) at the top level of `/workspace` was invisible to `inspect`, `axi benches`, and `status`. It is now discovered like any other bench, without disturbing existing bench numbers or labels

### Fixed
- **Docker Desktop reported as "not installed" on WSL2 when it was only stopped** - When Docker Desktop is stopped on the Windows host, its CLI tools disappear from WSL2 the same way as if Docker had never been installed, so cwcli pointed users at the install page instead of telling them to start Docker Desktop. WSL2 is now detected and given the correct instruction
- **`restore --send` no longer fails with a cryptic transfer error on a full disk** - `restore --receive` already checked for free space before downloading; `--send` now gets the same check before it stages files for transfer, so a full disk is reported plainly instead of surfacing as an opaque sendme failure
- **`cwcli open`'s interactive Docker shell no longer fights the calling console for input on Windows** - The handover relied on a POSIX process-replacement trick that Windows does not support, so the container shell and the launching PowerShell session could both read the same keystrokes. The shell now runs as a waited child process that Windows console ownership passes to cleanly

## [2.1.0] - 2026-07-29

Sendme transfers and restores no longer exhaust a small temp partition, long-running `init` phases stay visibly alive instead of looking hung, and a bare `cwcli` now prints help instead of a traceback. The published package itself is also clean for the first time under the new packaging allow-list: no test files ship in the artifact.

### Added
- **`cwcli axi url <project>`** - Reports the host address a bench actually answers on and probes it fresh with a real HTTP request, naming the site it asked for. Nothing else on the `axi` surface states the host-reachable address; `axi status`'s health probe is measured against the container-internal port and never surfaces the address a browser would use

### Changed
- **`cwcli restore --receive` validates before downloading** - The site and, in receive mode, the sendme ticket are now checked before a multi-gigabyte transfer starts, rather than failing after the download completes. Restore also no longer keeps a duplicate copy of the backup archive on disk during the process
- **Sendme transfers and restore no longer fight a small `/tmp`** - Transfers now stream through cwcli's own home directory instead of the system temp partition and stream rather than double-copying, so a large restore no longer exhausts a small temp partition. Receive mode also checks free space before the download starts. When disk space is genuinely the problem, cwcli now says so plainly instead of failing with an opaque I/O error
- **Long `cwcli init` phases stay visibly alive** - A bench build or dependency install can run for minutes with no output, which used to look identical to a hang. Long phases now announce themselves and keep progress visible for their duration

### Fixed
- **A bare `cwcli` prints help instead of crashing** - A runtime-only install can end up on Typer's vendored Click, whose error classes are not the ones cwcli's error handling caught by name, so a bare invocation raised an unhandled exception instead of showing the usage text every other zero-argument CLI shows
- **The published package no longer ships the test suite** - The published 1.1.0 sdist carried 102 test files, including a fixture with a private hostname, because the project had no packaging allow-list and setuptools' default swept `tests/` in. Packaging is now a deny-all allow-list: only the package itself and the required build inputs are included, so a newly added test or fixture directory is excluded by construction rather than by someone remembering to exclude it
- **`cwcli rm`, `cwcli restart`, `cwcli stop`, and `cwcli start` accept clustered and attached short options** - Forms Click accepts everywhere else, such as `-pweb` for `--process web` or `-vy` for `-v -y`, were refused as "No such option" on these four commands because they parse their own trailing arguments. A cluster that does not fully resolve is refused outright rather than partially applied, so a typo can never be silently read as consent to a destructive action
- **`cwcli axi` help output is concise TOON across every command** - Group listings, argument metavars, and command notes were truncated or leaked raw markdown in places; help across the whole `axi` tree is now rendered consistently
- **App changes fully resynchronise a running bench** - Installing, uninstalling, updating, or checking out an app used to restart only the web process, so the scheduler and background workers kept running the old code after a mutation landed. Every code-bearing process is now cycled, and the affected sites must answer a live ping before the change reports success
- **Bench listings stop vouching for a bench that no longer exists** - A removed bench directory kept reporting as present across repeated reads. Bench presence is now verified against the container and the host filesystem where that is possible, and honestly reported as unverified rather than guessed when it is not
- **A stranded migrate lock is now diagnosed instead of failing opaquely** - A migrate that loses a lock race can leave a genuinely held lock file behind; every later migrate against that site now fails with a clear pointer to `cwcli unlock` instead of the same unexplained error
- **Adding a bench to a running instance no longer risks restarting every other bench on it** - `cwcli init` against an already-running instance used to re-pull and potentially recreate the frappe container unconditionally, which could silently kill every other bench's supervisor. Adding a bench now never refetches or recreates anything
- **Bench numbers stay stable across additions** - A new bench sorting earlier by path used to renumber an existing bench's index the moment it was added, so a stored or scripted `--bench 0` could silently start addressing a different bench. Bench numbers are now durable identities assigned on first discovery and never reassigned
- **`cwcli init` no longer crashes on Windows during user-ID alignment** - The Linux/macOS-only ownership alignment step now no-ops cleanly on platforms without POSIX UID/GID support instead of raising

## [2.0.0] - 2026-07-23

Multi-bench instances are the theme. `cwcli status` now reports every bench on an instance and measures each one on its own port and its own site, instead of modelling a single bench and asking `localhost:8000` about all of them. `cwcli scale` lifts the six-bench ceiling that silently made a seventh bench unreachable from the host. `cwcli rm-site` drops one site without touching the instance around it, and agents gain the removal, install, and asset-build verbs they previously had to reach around cwcli to run. `cwcli run -i` finally carries bench commands that ask questions. **This is a major release because `cwcli axi status`'s document shape changed:** its per-bench fields moved inside `benches[i]`, including on single-bench instances. `apps checkout` also now genuinely refuses to run over uncommitted work, which the documentation always claimed it did.

### Added
- **`cwcli scale <project> [--to N] [--yes]`** - Widens an instance's published port range so every bench is reachable from your machine. An instance publishes six web and six socketio ports at `init`, and bench assigns each new bench the next port up, so a seventh serving bench binds its port inside the container and is silently unreachable from the host - no crash, no warning, just an address that never answers. `scale` reconciles the published range to cover every bench (reading each bench's own config as the truth about which port it serves, never a second store that could drift), then applies it by recreating only the frappe service. Your database is untouched: MariaDB, Redis, and the data volume are left alone. Expanding restarts every serving bench in the instance, because they share one container, so it asks first unless you pass `--yes`. A range that already covers every bench is a safe no-op. Also available as `cwcli axi scale`, where the confirmation becomes a usage error naming `--yes`
- **`cwcli rm-site <project> <site>`** - Permanently drops one named site from a running bench and leaves the instance, and every other site on it, running. Previously the only removal verb was `cwcli rm`, which destroys the whole instance. The site must be named explicitly; there is no default and no fallback. `bench drop-site` archives the dropped site's whole directory, including the database credentials in its `site_config.json`, inside the container and never prunes it, so this copies that archive out to the same managed location `cwcli rm` already uses and deletes the in-container copy only once the host copy is verified. If that copy cannot be verified the in-container archive is left in place and the command exits non-zero, rather than deleting credentials nobody has a copy of. Also available as `cwcli axi rm-site`
- **`cwcli run <project> -i`** - Forwards stdin to the bench command, so bench commands that ask questions work through the wrapper. `bench new-app`, `console`, and `mariadb` all prompt, and `cwcli run` attached nothing to their input, so they died on the first question. That broke cwcli's own advice: every bench command cwcli suggests is phrased as `cwcli run`, and the suggested path could not carry the command. It is opt-in rather than automatic, because turning it on for every run would hand ordinary commands a terminal they do not have today, and interrupting an interactive run exits 130 rather than reporting success
- **`cwcli axi rm <project>`** - The agent-facing form of `cwcli rm`, running the human command's own code: the same fail-closed backup gate, the same verified per-bench copy-out, the same exit code driven by what actually failed. Two things differ deliberately. `--yes` grants consent only, never the container auto-start the human `--yes` also grants, so an agent asking to delete an instance can never thereby start one. And there is no `--no-backup`; the backup gate has no off switch here, and the human command remains the way through. A stopped instance whose volumes are in scope cannot satisfy the gate, so it is refused before anything is touched. Every refusal names its ways out
- **`cwcli axi apps install <project> <app> --site <site>`** - Installs an app onto one named site. `--site` is required, unlike the human command's fan-out over every site on the bench, because installing runs the app's own `after_install` code against a live database and an unqualified fan-out is how an agent reaches a site nobody named. An app already installed on that site is refused before anything is fetched, naming `apps checkout`, `apps update`, and the human command as the ways forward; a site whose app list cannot be read is refused too, rather than treated as empty. There is no bypass flag
- **`cwcli axi build <project>`** - Compiles a bench's JavaScript and CSS assets. An app whose front-end changed is not visibly changed until its assets are rebuilt, so this completes the checkout, migrate, test loop that previously had no way to finish without dropping to a raw command. It touches no database and needs no site
- **`cwcli axi run-tests --module <dotted.module>`** - Narrows a test run to one module instead of an app's whole suite. It only ever reduces what runs, so it needs no guard of its own; `--app` stays required so the report still names the scope
- **`cwcli stop <project> --bench <index|label>`** - Stops one bench's dev processes and leaves the containers, and every sibling bench, running. It is the inverse of `cwcli start --bench`, which has always been per-bench, and it closes the one gap in a set where `start`, `status`, `restart --process` and `logs` all took `--bench`: previously, taking a single bench down on a multi-bench instance meant reaching past cwcli into the container with `docker exec ... supervisorctl`. Stopping a bench that is already stopped is a success, not an error, and a deliberate stop reports as a clean `online`, not a fault. Also available as `cwcli axi stop <project> --bench <index|label>`

### Changed
- **BREAKING: `cwcli axi status` reports every bench, and its per-bench fields moved inside `benches[i]`** - `supervisor_up`, `web_http_code`, `processes`, and `not_cwcli_supervised` now live under `benches[i]` rather than at the top of the document, including on a single-bench instance, where `processes` becomes `benches[0].processes`. **Anything parsing those fields at the top level needs updating.** The shape is now uniform, so there is one parse path rather than a branch on how many benches an instance has. A multi-bench instance with no `--bench` also now exits 0 with every bench reported, where it used to exit 2 and send you off to `cwcli axi benches` to poll once per bench and reassemble an instance view from documents that never said which bench they described. Each bench additionally carries `index`, `bench_path`, `label`, its own `overall`, the `web_port` its `web_http_code` was actually measured on with a `web_port_verified` flag, and the `web_site` it was measured for. A stopped instance carries `benches: []`
- **`cwcli status` reports every bench and probes each one on its own port** - `status` modelled exactly one bench and asked a hardcoded `localhost:8000` about it, so on any bench past the first it measured a *different* bench's web server. A fully healthy second bench reported `degraded` while its own web process read `RUNNING`, and a bench serving nothing reported its neighbour's live response as its own. Ports now come from each bench's own configuration. When a bench's port cannot be read, cwcli reports that it does not know and makes no probe, rather than falling back to `:8000` and describing the wrong bench; a bench in that state is not counted as unhealthy. `cwcli start` no longer spends its 60 second readiness wait on the wrong port either
- **`cwcli apps checkout` refuses to run over uncommitted work** - The documentation always said a dirty working tree makes `apps checkout` fail. It did not. The command leaned on git's own refusal, which covers only a checkout that would overwrite a modified file, so an edit the target branch happens not to touch was carried silently across the switch and the command reported success. **This is a behaviour change you can trip over:** a checkout that used to succeed over local edits now exits non-zero and names the paths it found. Uncommitted means what `git status` means, including files you have created but not yet added, because a new module written but not yet staged is uncommitted work in the plainest sense and is exactly the case where cwcli must not decide on your behalf that a file is worthless. Ordinary build residue is ignored by `.gitignore` and does not trigger it. `--reset` remains the explicit way through, and it discards tracked edits but does not delete untracked files, because cwcli never runs `git clean`
- **`cwcli rm` also removes the instance's own Docker network** - Every removed instance used to leave its `<project>_default` network behind forever, and each one consumed part of Docker's finite address pool until some unrelated command failed with a pool exhaustion error you would have no reason to connect to cwcli. The network is now removed by exact project label, never by a prune that could catch something else, and regardless of `--no-volumes`, since a network holds no data of yours. A network with something from outside the project still attached is reported and left alone rather than forced. Both `cwcli rm` and `cwcli axi rm` report whether it was removed
- **`cwcli inspect` says whether an app's version is verified or remembered** - A tiered read served each site's `installed_apps`, which carry an app's version and git branch, from the cache without re-observing them, while labelling the read as freshness-checked. A checkout inside an app changes nothing the cheap tiers look at, so a stale branch rode along looking current, which is the kind of thing that leads a test run to validate the wrong code and pass. Every site now carries `installed_apps_verified`, true only when a full read actually re-ran the query, and a read serving a remembered list warns and names `--update`

### Fixed
- **A misspelled option no longer silently retargets `start`, `stop`, `restart`, or `rm`** - These four take a variadic list of projects, which greedily swallows every token written after it, options included. Each one recovered the flags it knew and then treated whatever was left as another project name, so an option it does not define disappeared without comment: `cwcli stop myproj --benhc 1` stopped the whole instance, reported only that two projects named `--benhc` and `1` could not be found, and never mentioned that the flag was unrecognised. A typo, or a flag that is valid on a sibling command, could quietly point a destructive command somewhere you did not mean. An unrecognised option is now a usage error that names the option and points at the command's help, raised before any project is touched. A leading dash alone does not make a token an option, so a bench label that starts with one still works (`--bench -staging`, `--bench=-1`), and `cwcli start` now reports a `--bench` given without a value instead of dropping it
- **`cwcli restart --bench N` restarts the bench you named** - On a multi-bench instance the selector reached only the `--process` path. A whole-stack `cwcli restart <project> --bench 1` accepted the flag without complaint, took every container down, and then brought up a *different* bench - leaving the one you named dead, at exit 0, with nothing reported. `start`, `status` and `logs` all honored the same flag, which is what made this a trap rather than a documented limit
- **`status` no longer reports an alarming `404` for a perfectly healthy bench** - Frappe is multi-tenant and routes by the `Host` header, so cwcli's health probe, which named no site, was correctly answered `404` by a bench that serves `200` for its actual site. The number was meaningless and it appeared on every read; a field whose normal value is an error code teaches you to ignore it, and that habit is exactly what would make a genuine `degraded` go unread. The probe now names the bench's site and a healthy bench reads `200`. `status` also reports which site it asked for (`web_site`, shown as `web <site>:<port> -> <code>`), so the code stays attributable rather than reading as a claim about the whole bench
- **`cwcli init`'s success banner and `cwcli open` print an address that actually works** - The banner hardcoded `http://<site>:8000`, which is a *container* port printed as if it were a host port: an instance created with `--port 21000` advertised an address nothing answers on, and every bench past the first serves its own assigned port anyway (bench 1 is `:8001` inside, published as `:21001`). Both hops are now resolved from the bench's own config and the instance's live published ports, and `cwcli open` reports the same address - previously it modeled no port at all, so the banner's own suggested alternative could not help either. An address that cannot be read is omitted rather than guessed
- **`cwcli where` no longer vouches for an instance that is gone** - `where` answers from a cache that outlives the instances it describes, and it served a removed project's apps and sites exactly as it served live ones, with nothing marking them as remembered. It returned the same confident answer while the Docker daemon was unreachable, the one moment it could have known cheaply that it was guessing. Every match now carries `project_state` (`present`, `absent`, or `unverified`) and the result carries `verified`, checked against the live instance list. The check is one Docker call per run no matter how many matches there are, and it never reaches into a bench, so it does not re-incur the cost the cache exists to avoid. An unreachable daemon reports `unverified` rather than reporting that everything is gone. `--no-verify` skips the check and says so instead of pretending. A stale entry is reported, never quietly deleted; refreshing is `cwcli inspect`'s job
- **`cwcli init --reuse-bench` no longer reports a port conflict against the instance it is reusing** - Adding a bench or a site to a running instance is the ordinary way to use `--reuse-bench`, and every port the check found taken was taken by that very instance. It refused and advised `--port`, which is advice you cannot follow, since an existing instance's ports are fixed in its compose file. The check is now skipped when the project has a container of its own already running, and still runs for a stopped or absent project, where a taken port genuinely belongs to somebody else

## [1.1.0] - 2026-07-20

Private app repos now just work, and you can put a specific branch of an app under test without leaving cwcli. Installing or updating an app from a private GitHub or GitLab repo no longer needs a token pasted into a container - cwcli borrows the login you already have on your machine. `apps checkout` switches one app in a bench to any branch, tag, or commit, and `cwcli migrate` and test runs are now available to agents as their own commands instead of raw pass-through. `cwcli --version` also tells you which build you are actually running.

### Added
- **Private GitHub/GitLab app repos work with no token setup** - `cwcli apps install`, `cwcli apps update`, `cwcli apps checkout` (and the deprecated `cwcli update`) now authenticate git fetches against private app repos using the `gh`/`glab` login you already have on your host machine. Nothing to configure: sign in on the host (`gh auth login` / `glab auth login`) and private repos fetch like public ones. No token is ever written into the container or stored on disk, and nothing changes for public repos
- **`cwcli apps checkout <project> <app> <ref>`** - Puts one named branch, tag, or commit into an app that is already in the bench, so a feature branch can be tested in the instance the app lives in. This is the gap the other verbs left: `apps install` only does a fresh clone, and `apps update` only follows each app's tracked upstream. The app's git remote is auto-detected, so it works on both bench-installed and hand-cloned apps. `--reset` additionally discards local edits in the container's copy, which is what a following build or migrate needs
- **`cwcli axi apps checkout`** - The agent-facing form of the above, emitting the same per-step report as the other `axi apps` verbs, one row per git step
- **`cwcli axi migrate <project> [--site]`** - Runs `bench migrate` for one site as its own command. Previously the only way an agent could migrate was `cwcli axi apps update`, which pulls every named app first - so it moved the very branch you had just checked out. This migrates exactly one site and pulls nothing. The site is put into maintenance mode for the duration; if it cannot be, the migration does not run, and a site left in maintenance mode is reported as a failure rather than quietly left down
- **`cwcli axi run-tests <project> --site <site> --app <app>`** - Runs an app's test suite against a site. Both the site and the app must be named explicitly: a test run executes the app's own code against that site's live database, so cwcli will not pick a target for you
- **`cwcli --version` identifies the build, not just the version number** - Output is now e.g. `Version: 1.1.0 (release build)`, `(source build, git 1a2b3c4)`, or `(editable source build, ...)`, with `dirty` marking uncommitted changes. A version number alone cannot distinguish a published release from a local build that happens to carry the same number, which previously led to a wrong conclusion about what a build contained. The `Version: <number>` prefix is unchanged, so anything already parsing it keeps working

### Fixed
- **Documentation overstated `apps checkout`'s protection of uncommitted work** - The docs said a dirty working tree makes the checkout fail. It does not in general: git refuses only when the checkout would overwrite a modified file, and a modified file the checkout does not touch is carried across and the command succeeds. The wording now describes what actually holds, since the previous claim could lead someone to leave uncommitted work in an app directory believing the tool protected it. `--reset` remains the explicit opt-in for discarding local changes

## [1.0.0] - 2026-07-18

caffeinated-whale-cli's first stable release. It brings a first-class, agent-facing `cwcli axi` command surface built on a new UI-pure logic core - business logic separated cleanly from the terminal UI, so the human CLI, agents, and any future GUI all run over one implementation - replaces honcho with a per-process **supervisord** supervisor for real per-process health and self-heal, adds first-class `apps` management, and rounds out the instance lifecycle so `cwcli init` leaves a running dev environment. Everything since 0.35.0 - including the never-published 0.36.0 and 0.37.0 changes - ships together here.

### Added
- **`cwcli axi` command group** - An agent-facing surface over cwcli's new UI-pure logic core. Its output is structured [TOON](https://toonformat.dev) on stdout (progress and diagnostics go to stderr), it never prompts, and it maps outcomes to conventional exit codes (0 success, 1 error, 2 usage). Bare `cwcli axi` is a content-first home that prints the binary path, a one-line description, the live Frappe instances, and a few next-step commands. `cwcli axi backup <project> [--site <site>] [--bench <index|label>] [--with-files]` performs the same backup as `cwcli backup` and emits the outcome as TOON; a decision it cannot make from flags (an ambiguous multi-bench project, a stopped instance) becomes a structured usage error naming the flag to pass, rather than an interactive prompt
- **`cwcli axi start` / `cwcli axi status`** - Agent-facing verbs over the same core as `cwcli start`/`cwcli status`. `cwcli axi start <project> [--bench] [--yes]` starts the containers + bench and emits the `StartOutcome` (including `already_running`) as TOON; it never prompts - an ambiguous multi-bench project is a `--bench` usage error (exit 2) and an unresolved port conflict is a `CONFLICT` error naming `--yes` (which auto-resolves conflicting Frappe projects). `cwcli axi status <project> [--bench]` emits the health report as TOON with the `overall` aggregate up front (always exit 0)
- **Per-process supervisor (`cwcli restart --process`, auto-heal)** - The bench now runs under **supervisord** instead of honcho (installed into the bench env on first `cwcli start`), so a single crashed Procfile worker can be restarted or self-heal while the rest of the bench keeps running - honcho's all-or-nothing model made this impossible. `cwcli restart <project> --process <label> [--bench]` restarts one program (e.g. `web`, `worker`, `socketio`) leaving its siblings running; without `--process` it stays the whole-stack restart. Crashed programs **self-heal** by default (supervisord `autorestart`, with a crash-loop surfacing as a visible `FATAL` rather than a silent hot loop); `cwcli start --no-autorestart` disables it. `cwcli axi restart <project> --process <label>` emits the `ProcessRestartOutcome` (`label`, `old_pid`, `new_pid`, `supervisor_state`) as one TOON document
- **`cwcli axi restart`** - Agent-facing single-program restart over `core.restart_process`; `--process` is required, an unknown/ambiguous process is a usage error listing the valid labels (exit 2), and a multi-bench project with no `--bench` names `--bench` (exit 2)
- **`cwcli axi benches` / `cwcli axi label`** - Agent-facing verbs over the same core as `cwcli label`. `cwcli axi benches <project>` lists a project's benches with their indices, paths, and user labels: it is the structured answer to "multiple benches; pass `--bench <index|label>`", which every bench-scoped verb asks and which nothing on the agent surface could previously answer - an agent had to fall back to the human `cwcli inspect`. A project that has never been inspected is a structured error naming `cwcli inspect`, not an empty list, since "not inspected yet" and "has zero benches" are different facts. `cwcli axi label <project> [--bench <index|label>] --set <label> | --clear` sets or clears a bench's durable label and emits the `LabelOutcome` as TOON; exactly one of `--set`/`--clear` is required, and it never starts a stopped project (the label marker is stored inside the bench)
- **`cwcli axi inspect` / `cwcli axi logs`** - Read-only agent verbs over the same core as `cwcli inspect`/`cwcli logs`. `cwcli axi inspect <project>` performs one tiered freshness read and emits the report as TOON (no `--yes`, never auto-starts). `cwcli axi logs <project> [--bench] [--lines/-n N] [--process/-p]` returns a bounded `tail -n N` of the bench's per-process logs as one TOON document (a metadata head then one raw-line block per process; no `--follow`); a running-but-quiet bench is a successful empty read (exit 0), while a stopped project is a usage error naming `cwcli start`
- **`cwcli axi init` / `cwcli axi apps list` / `cwcli axi config`** - `cwcli axi init` provisions an instance end-to-end and emits one terminal `InitReport` TOON document, taking the admin/db-root passwords from `CWCLI_ADMIN_PASSWORD`/`CWCLI_DB_ROOT_PASSWORD` (recommended, off the argv) or `--admin-password` (never generated or prompted; a missing password is a usage error naming both). `cwcli axi apps list <project>` is the agent read verb for a bench's available/installed apps. `cwcli axi config <project>` is the single read-only config verb (search paths, auto-inspect state, file locations)
- **`cwcli axi self-update --check`** - A read-only agent-facing version check over the same core as `cwcli self-update --check`. `--check` is required, so the verb can never upgrade. It **exits 0 on any successful read, including when an update is available**, and carries the answer in the `is_outdated` field: on the agent surface a non-zero exit means an error, and a read that successfully reports "you are outdated" has not failed (the same reason `cwcli axi status` exits 0 while reporting an offline project). This deliberately differs from the human `cwcli self-update --check`, which still exits 1 when an update is available so shell scripts can gate on it. An unreachable PyPI fails open (`latest: null`) and still exits 0. The mutating `cwcli axi self-update` remains deliberately deferred
- **`cwcli apps update --json` / `cwcli axi apps update`** - `apps update` was the only `apps` subcommand without machine-readable output; both surfaces now emit the full per-phase report (which apps updated, which sites were affected and actually migrated, and every per-phase failure). Stdout carries only the document - bench's own output can never corrupt it, including when updating the `frappe` framework, which previously wrote `bench update --reset`'s output to stdout whatever you asked for. `cwcli axi apps update <project> <apps...>` blocks until the update finishes and emits ONE TOON document, exactly as `cwcli axi backup` does for a minutes-long backup; it exits 0 only when the report's `ok` is true, and an ambiguous multi-bench project is a `--bench` usage error (exit 2). A stopped project is likewise a usage error naming `cwcli start` (exit 2) - the agent verb has deliberately no `--yes`, since starting a container is UI-coupled. **`failed_*` and `unknown_*` are different and must not be collapsed:** a failed step ran and failed, so retrying is safe, while an unknown step's output stream was lost - it may still be running, so check before retrying
- **`cwcli axi unlock` / `cwcli axi stop`** - Agent-facing verbs over the same core as `cwcli unlock`/`cwcli stop`. `cwcli axi unlock <project> [--site] [--bench]` removes a site's locks folder and emits the `UnlockOutcome` as TOON, with the removed paths as a structured `removed` list and a site that was not locked reported as a clean `already_unlocked: true` success; it follows `cwcli axi backup`'s conventions exactly (multi-bench without `--bench` is a usage error, a stopped instance points at `cwcli start`). `cwcli axi stop <project>` stops a project's containers and emits the `StopOutcome` as TOON; it is idempotent for an agent, so an already-stopped project is a definitive success (`already_stopped: true`) rather than an error
- **Making agents aware of the `cwcli axi` surface** - Two complementary ways an agent learns the surface exists. `cwcli axi setup` installs a SessionStart hook for Claude Code, Codex, and OpenCode that announces the live surface each session; and cwcli ships an installable Agent Skill (`npx skills add karotkriss/caffeinated-whale-cli --skill cwcli`) that loads on demand in any agent. A user installs either or both
- **`cwcli init` auto-starts dev services** - After creating the bench+site, `cwcli init` now starts the bench's dev services by default (reusing `core.start`, the same path behind `cwcli start`), leaving a running dev environment instead of a created-but-idle one. A create-only `--start`/`--no-start` flag opts out, distinct from `--auto-start` (which controls only Docker container startup); a service-start failure degrades to a warning rather than failing init, since the bench is already created. The completion message now reflects the running state with open/logs/stop/restart guidance. `cwcli axi init` gains the same auto-start-by-default behavior and `--no-start` opt-out
- **`cwcli self-update` command** - Install-method-aware upgrade of cwcli itself (`uv tool upgrade` or `pip install --upgrade`, chosen from how cwcli was installed; a dev/editable checkout or an ephemeral `uvx` run is a no-op). `--check` is a read-only dry run reporting current-vs-latest and exiting non-zero when an update is available; `--no-cache` forces a fresh PyPI lookup. Named `self-update` because the top-level `update` is the deprecated Frappe-app updater
- **Passive update-available notice** - A one-line "a newer cwcli is available" hint printed at most once a day. It is stderr-only and shown only on a TTY, so it never corrupts machine-readable stdout or appears in pipes/CI/agent runs; it never blocks on the network (a background refresh populates a ≤1-day cache for the next run) and is suppressible with `CWCLI_NO_UPDATE_CHECK=1`
- **`cwcli status --watch`** - A live, continuously-refreshing per-process view of a bench's health that deliberately never probes the web server, for watching a stack come up or recover
- **`restore --receive --ticket <sendme-ticket>`** - A non-interactive path for receiving a peer-to-peer backup: pass the sendme ticket as a flag instead of at a prompt. A non-TTY `--receive` without `--ticket` now refuses with a non-zero exit naming the flag, instead of silently exiting 0 as a no-op
- **`CWCLI_HOME` environment override** - Set `CWCLI_HOME` to relocate cwcli's entire on-disk footprint (projects, config, cache, runtime/PID files, and the `rm` pre-deletion archive directory) out of `~/.cwcli` and into a directory of your choice; when set, cwcli uses `$CWCLI_HOME/projects`, `$CWCLI_HOME/config`, `$CWCLI_HOME/cache`, and `$CWCLI_HOME/run`. Unlike repointing `HOME`, it redirects only cwcli's own state (leaving `git`/`ssh` and other `HOME`-derived tools untouched), and when unset (or empty) cwcli uses the default `~/.cwcli` locations. The relocated cache keeps the same restrictive permissions (`0700` directory, `0600` database file)
- **`apps` command** - First-class Frappe app management, replacing the raw `cwcli run <project> bench ...` escape hatch with per-bench/per-site addressing, `--json` output, and honest exit codes
  - `cwcli apps list <project>` lists the apps available in a bench (live `ls apps/`); `--installed`/`--site` also lists the apps installed per site (all sites by default, grouped by site)
  - `cwcli apps install <project> <app...>` fetches (`bench get-app`, optional `--branch`) and installs app(s); each app is a known name **or** a git URL; `--fetch-only` fetches without installing
  - `cwcli apps uninstall <project> <app...>` removes app(s) from sites (destructive; gated by `-y`/`--yes` or an interactive confirmation)
  - `cwcli apps update <project> <app...>` is the canonical app-update path; updating `frappe` runs `bench update --reset`
  - **Multi-site by default** for install/uninstall/update: with no `--site` the command applies to every site on the bench; `--site` is repeatable and narrows. The fan-out runs per site, aggregates results, and exits non-zero if any site fails (with a per-site report) - a partial failure is never hidden behind a success banner
  - `list`, `install`, and `uninstall` support `--json` (`apps update` delegates to the streaming update flow); every subcommand honors the non-interactive contract (a non-TTY without the required flag refuses non-zero; auto-start gated by `--yes`) and refreshes the cache after a mutation so `where`/`open`/`inspect` reflect the new state

### Changed
- **Distribution via `uv` tooling** - cwcli now installs, runs, and upgrades cleanly as a uv tool: `uv tool install caffeinated-whale-cli` (unpinned, so `uv tool upgrade` works) or `uvx --from caffeinated-whale-cli cwcli ...`. Both the `cwcli` and `caffeinated-whale-cli` console scripts are provided
- **`cwcli init` version handling** - Defaults Frappe to `version-16` and adds a `--version <N|X.Y.Z>` alias that resolves to the matching `version-N` branch or `vX.Y.Z` tag (the existing `--frappe-branch` still takes a raw ref; the two are mutually exclusive). Python/Node versions and compatibility pins are selected from the major version
- **`cwcli init` maps a persistent workspace** - A fresh instance's bench workspace is now a per-project host directory (under the project's `data/`) rather than living only inside the container, so the bench survives container recreation; a re-init guards against a mount mismatch with an existing instance's frozen compose
- **`cwcli init` secret handling** - `bench new-site`'s admin and db-root passwords are passed via the environment rather than on the command line, so they no longer appear on the container process list or in echoed commands. When `--admin-password` is omitted in an interactive run, a strong admin password is generated and printed once (it is required, not generated, non-interactively)
- **Suggested commands point at cwcli** - Every `bench`/`docker exec` command cwcli suggests in a hint, error, or success message is now phrased as the copy-paste-accurate `cwcli run <project> [--site <site>] ...` wrapper (or the matching cwcli verb), never a raw in-container command the user cannot run directly
- **`cwcli backup`** - Re-seated on the shared logic core (`core.backup`) with no change to its behavior: identical flags, prompts, success banner, error messages, and exit codes. This is the first command migrated onto the core; the human CLI stays a thin frontend and the E2E backup proof stays green unchanged
- **`cwcli unlock`** - Re-seated on the shared logic core (`core.unlock`) with no change to its flags, prompts, or exit codes. A site that was not locked is now reported honestly as "already unlocked" rather than as a generic success. With `--verbose`, the removed paths are still listed, but they now print at completion (from the structured result) instead of streaming line by line as the removal runs; for a locks folder the removal takes milliseconds, so the two are indistinguishable in practice
- **`cwcli label`** - Re-seated on the shared logic core (`core.label`) with no change to its arguments, flags, messages, or exit codes. `cwcli label <project> --verbose` now reports the resolved marker path and the operation's notes; previously `--verbose` accepted the flag but did nothing at all
- **`cwcli run`** - Re-seated on the shared logic core (`core.run_plan` + `core.exec_stream`) with no change to its arguments, flags, or prompts. Its exit code is now honest (see Fixed), and `--verbose` reports the resolved bench and the command being run. Documentation now states that bench's own flags (`--site`, `--branch`, ...) must follow a `--` separator, since `cwcli run` parses its own options first - the previously documented `cwcli run frappe-one --site development.localhost migrate` never worked and exited 2
- **`cwcli apps update` / `cwcli update`** - Re-seated on the shared logic core (`core.update`) with no change to either command's arguments, flags, prompts, or exit codes; both remain thin frontends over one implementation, and the deprecated `cwcli update` keeps taking apps as `--app`/`-a` while `cwcli apps update` takes them positionally. **One deliberate behavior change:** when the connection to a bench command's output stream is lost, the update no longer abandons the remaining sites. Previously a migration that *returned* a failure was recorded and the fan-out continued, while a migration whose *stream was lost* stopped the run - the same real-world event behaving two ways depending on whether Docker happened to record an exit code. Both now continue, and the lost step is reported as **unknown** rather than failed: its outcome is genuinely unknowable and it may still be running, so it needs checking rather than a blind retry
- **`update` command** - Deprecated in favor of `cwcli apps update`. It keeps working (and now emits a deprecation notice) as an alias, gains a repeatable `--site` to narrow which affected sites are migrated (refusing non-zero if the named site(s) match none of the actually-affected sites), and updating the `frappe` framework app now runs `bench update --reset` (a bench-wide reset, so `--site` and the per-app migration flags are reported as ignored on that path)
- **`cwcli stop`** - Re-seated on the shared logic core (`core.stop`) with no change to its behavior: the same messages, the same per-project honest exit code, and the same multi-project/stdin handling. An unreachable Docker daemon is now reported as a connection error rather than being misreported as "project not found"
- **`cwcli start`** - Re-seated on the logic core (`core.start`) and now **idempotent**: re-running against an already-running bench reports "already running" and does nothing, instead of spawning a second supervisor stack. It launches **supervisord** over the bench's dev Procfile (installing `supervisor` into the bench env on first supervise), gaining per-process restart and self-heal (see the per-process supervisor entry above); `--autorestart`/`--no-autorestart` sets the self-heal state at launch. On a multi-bench project with no `--bench` it now prompts which bench interactively and refuses (non-zero) on a non-TTY, matching the rest of the command family (previously it silently used the first bench)
- **`cwcli status`** - Re-seated on the logic core (`core.status`) and enriched from a single blind `curl` into real per-process health: per-Procfile-process up/down with PID, uptime, CPU%, RSS, and supervisord's authoritative `state` (RUNNING/STARTING/BACKOFF/EXITED/FATAL/STOPPED), plus the web HTTP probe. stdout stays a single aggregate token (`offline`/`online`/`running`/`degraded`); because supervisord keeps siblings alive when one dies, `degraded` now honestly covers a stable partial stack (e.g. web serving while a worker is `FATAL`) in addition to a down supervisor or an unanswering web. The per-process breakdown prints on stderr, and the full structured report is available via `cwcli axi status`. When a bench is running but **not under cwcli's supervisord** - a plain `bench start`, or an instance started before the supervisor landed - `status` no longer reports every process as down: it falls back to detecting the honcho / `bench start` process tree, reports each process's true `up`/PID/uptime, flags the report `not_cwcli_supervised` with a hint to run `cwcli start`, and aggregates to the honest `running`/`degraded`. The fallback is a pure read (it never launches supervisord), and it applies to `cwcli status`, `cwcli status --watch`, and `cwcli axi status` alike
- **`cwcli logs`** - Now reads supervisord's per-process log files under `<bench>/logs/<program>.supervisor.log` (with built-in rotation) on the workspace volume, replacing the single honcho combined stream. `cwcli logs <project>` synthesizes a combined, label-attributed view; `cwcli logs <project> --process <label>` tails one program's log; it also gains a `--bench` selector for multi-bench projects. When a bench is running but not under cwcli's supervisord, `logs` falls back to discovering and tailing the bench's real `logs/*.log` files instead of falsely reporting no logs
- **Test suite** - Reorganized into two tiers selected by pytest marker: a fast `unit` tier (the default; needs no Docker) and a real-Docker `e2e`/`e2e_p2p` tier under `tests/e2e/` that drives the real `cwcli` binary against genuine throwaway Frappe instances. A bare `pytest` runs only the fast tier; `pytest -m e2e` runs the real-Docker tier (add `pexpect` via the new `e2e` extra: `uv sync --all-extras`). The E2E harness isolates every run under a temporary `HOME` + `CWCLI_HOME` with `cwe2e-`-prefixed names, hard safety rails, and an unconditional leaked-resource backstop, and CI runs it on a v14/v15/v16 matrix on GitHub-hosted runners

### Removed
- **`cwcli inspect --show-apps` / `-a`** - Removed the dead flag: it was declared but never read, and the tree output has always shown available apps regardless. Passing it now fails as an unknown option (exit 2); the output it advertised is what `cwcli inspect` already prints

### Fixed
- **`cwcli start` reports running only once the web server is up** - `start` now waits for the web server to actually bind `:8000` before reporting the bench running, instead of declaring success while the web process was still starting (a timeout is surfaced as a warning)
- **`cwcli rm` works on any host (container/host UID alignment)** - `cwcli init` now aligns the container's `frappe` user to the host UID before provisioning, and `cwcli start` re-aligns on every launch, fixing a `[Errno 13] Permission denied` that could leave `cwcli rm` unable to clean up the workspace on some hosts (notably CI); a fresh compose no longer creates a root-owned working directory under `CWCLI_HOME`
- **`cwcli rm` backs up every bench before deletion** - On a multi-bench instance, `rm` now verifies a backup of *all* benches (each in its own namespaced archive directory) before removing any volumes, and a config-archive failure warns instead of blocking the cache clear

## [0.35.0] - 2026-07-09

### Added
- **`restore` command** - Non-interactive backup selection so a restore can run to completion with no prompts
  - `--latest` selects the most recent backup set for the target site, bypassing the selection menu
  - `--backup-file <name-or-path>` selects a backup set by its database filename (or full container path); the two flags are mutually exclusive and neither applies to `--send`/`--receive`
  - `-y`/`--yes` now also skips the confirmation prompts on the normal restore path (previously `--receive` only), so `--latest`/`--backup-file` + `--yes` + `--mariadb-root-password` runs fully unattended
- **`init` command** - `--reuse-bench` / `--no-reuse-bench` to pre-answer the existing-bench question non-interactively: `--reuse-bench` reuses the bench and skips `bench init`, while `--no-reuse-bench` requires a fresh `--bench` name and errors if it already exists (distinct from `--auto-start`, which only controls container startup)
- **`logs` command** - `-y`/`--yes` to auto-start stopped containers without prompting

### Changed
- **Cache never stores secrets** - Site and common site configurations are whitelist-filtered before being written to the cache, so database passwords, per-site encryption keys, admin/root passwords, and Redis URLs are stripped and never persisted (nothing reads them back; every credential consumer reads live from the container or from CLI flags/prompts). Any cache written before this shipped is cleaned in place on the next run, and the restricted filesystem permissions remain as defense-in-depth
- **Honest exit codes** - Commands now return a non-zero exit code when they do not do what was asked, instead of reporting success
  - `start`, `stop`, and `restart` exit non-zero for a nonexistent instance (never printing "started"/"stopped" for it); a multi-instance run processes every name and then exits non-zero if any failed
  - `config` error paths (invalid interval, "not enabled", no cache-clear target, and other failures) exit non-zero, while already-in-the-requested-state no-ops stay success
  - `update` reports failure and names the site when disabling maintenance mode fails after an update
  - `open` exits non-zero when it cannot pick an editor non-interactively (multiple editors and no `--code`/`--cursor`/etc. flag) or when the selection is cancelled

### Fixed
- **`cwcli apps update <project> frappe` wrote bench output to stdout whatever you asked for** - Updating the `frappe` framework app runs `bench update --reset`, and that path streamed the command's output unconditionally, ignoring `--verbose` entirely. Without `--verbose` the reset now runs under a progress spinner like every other step, and machine-readable output (`--json`, `cwcli axi apps update`) stays clean
- **Honest exit codes for `cwcli run` and `cwcli apps`** - Both could report **success for a bench command that never finished**. Docker reports an exec's exit code as `null` while it is still running, and a dropped connection to the output stream is indistinguishable from the command ending normally - so `cwcli run` exited `0` and `cwcli apps uninstall` reported `"ok": true` for work that may never have happened. Exit codes are now read only once Docker actually reports one; when the stream is lost, both commands say so and exit non-zero instead of claiming success. `cwcli run` now exits with the bench command's own code in every case
- **Non-interactive safety** - When containers are not running, `run`, `backup`, `update`, `open`, `unlock`, `logs`, and `inspect` now refuse with a non-zero exit in a non-interactive session (naming `--yes`) instead of hanging on a hidden prompt or crashing; pass `-y`/`--yes` to auto-start
  - `restore` refuses with a non-zero exit (naming the flag to pass) when a non-TTY run is missing a backup selector or a confirmation, instead of silently exiting `0` or hanging
  - `start`'s port-conflict confirmation likewise refuses on a non-TTY without `--yes` instead of hanging
- **`init` command** - No longer prompts under the live setup spinner when a container is slow to start (a bounded silent readiness poll runs inside the spinner and only escalates to a prompt afterward); a non-interactive run where the bench already exists now refuses honestly with exit 1 instead of hanging

## [0.34.0] - 2026-07-05

### Added
- **Multi-bench support** - A single project can hold more than one bench directory; you can now address an individual bench by numeric index or a durable label
  - `--bench <index|label>` selector on `run`, `backup`, `update`, `open`, `unlock`, `restore`, and `start`
  - `inspect` shows each bench with its numeric index (e.g. `Bench [1] at /workspace/frappe-bench-2`) and any label; the index is positional while a label is a stable handle
- **`label` command** - Assign, clear, or list per-bench labels for a project
  - `cwcli label <project>` lists the benches with their indices and labels (read-only)
  - `cwcli label <project> <selector> <new-label>` sets a label; `--clear` removes it
  - Labels are stored in both the cache and a marker file inside the bench, so `cwcli inspect --update` rebuilds them even after the cache is cleared
- **`inspect` command** - `-i`/`--interactive` now prompts for a durable label per bench and persists it (cache + marker), and `-y`/`--yes` auto-starts stopped containers without prompting
- **`restore` command** - `--no-migrate` flag to skip the post-restore `bench migrate` and instance restart
- **`--yes`/`-y` flag** - Added to `run`, `backup`, `update`, `open`, and `unlock` to auto-start stopped containers without prompting, to `config cache clear --all` to skip its confirmation, and to `start` to auto-confirm stopping conflicting projects

### Changed
- **Multi-bench data commands** - When a project has more than one bench and no `--bench`/`--path` is given, `run`, `backup`, `update`, `open`, `unlock`, and `restore` now stop and list the benches instead of silently operating on the first one
  - `start` keeps working by running `bench start` in the first bench and printing a note listing the others; pass `--bench` to start a different one
  - `-p`/`--path` remains an escape hatch for an explicit bench directory and cannot be combined with `--bench`
- **Default site resolution** - `backup`, `restore`, and `unlock` now resolve the default site from `sites/currentsite.txt` (written by `bench use`) when `common_site_config.json` has no `default_site` key, and `inspect` marks `(default)` from either source
- **`restore` command** - After a successful restore, runs `bench migrate` and restarts the instance so the restored database is brought up to the code's schema and the app comes back up cleanly (skip with `--no-migrate`; a failed migrate is surfaced and exits non-zero but does not undo the restore)
  - `--no-recache` is now a deprecated no-op: the missing-apps check reads app availability live from the bench instead of the cache

### Fixed
- **`restore` command** - `--receive` mode now passes the full backup path to `bench restore`, fixing the "Invalid path" failure when restoring on Frappe `version-14`
  - Interactive MariaDB credential prompts (username and password) are now collected correctly instead of being skipped or read as empty after the confirmation prompt
  - Restored the warning when the selected backup needs apps this bench does not have, now read from the backup's own database dump so a missing app is caught before the restore
- **`inspect` command** - No longer mistakes `sites/currentsite.txt` for a site (which caused a "Site currentsite.txt does not exist!" error); sites are detected by looking for a real site directory rather than filtering known non-site names

## [0.33.0] - 2026-07-04

### Added
- **`restore` command** - `--yes`/`-y` flag to skip the confirmation prompt in receive mode, for non-interactive use

### Changed
- **`restore` command** - Receive mode (`--receive`) now warns and asks for confirmation before the destructive `bench restore --force` that replaces all data in the target site
  - Refuses to proceed in a non-interactive session unless `--yes` is passed, instead of restoring silently
  - Warns when the backup's origin site does not match the site being restored into
  - Streams the backup into the container instead of buffering it in host memory, so large `--with-files` backups no longer spike memory
- **`rm` command** - Refuses to delete a project's named volumes and databases unless a verified, non-empty backup has landed on the host; pass `--no-backup` to remove without one
  - Streams each backup artifact out of the container instead of buffering it in host memory, so large `--with-files` backups no longer spike memory

### Fixed
- **`restore` command** - Database and admin passwords are no longer exposed on the container process list during a restore
  - A failed copy of the backup into the container now fails clearly instead of surfacing a confusing "backup not found" later
- **`rm` command** - Validates the project name before any deletion, rejecting empty, `.`, `..`, absolute, or path-separator names that could escape the projects directory
  - Returns a non-zero exit code when a removal only partially succeeds, instead of reporting success

## [0.32.0] - 2026-07-02

### Added
- **`inspect` command** - `--no-refresh` flag to return cached data as-is, skipping the freshness pass entirely

### Changed
- **`inspect` command** - 3-tier freshness model (cache / partial / full) so cached reads stay fast but no longer go stale
  - By default, a cheap read-only pass re-checks the apps and sites of known benches and escalates to a full re-inspect only when they drift from the cache, so a freshly installed app now shows up without `--update`
  - Serves cached data directly when the project's containers are not running, without prompting to start them
  - No longer rewrites the cache on every read; the cache is only written when a full inspect runs

### Fixed
- **`open` command** - `--app` now sees freshly installed apps instead of failing with "App not found" on a stale cache, and matches apps against the selected bench rather than the first one
- **`rm` command** - Removal now actually deletes the project's named Docker volumes (e.g. databases) and the local project directory instead of silently leaving them behind
  - Archives only `conf/` (the generated compose config) before deletion, so dangling bench symlinks can no longer abort the archive and block removal
  - Cleans up orphaned projects whose containers are already gone, still archiving config and removing leftover volumes and the project directory
  - No longer hangs on a stopped project: the pre-removal recache is skipped with a warning instead of deadlocking on a hidden prompt
  - Accepts trailing flags after the project name (e.g. `cwcli rm myproject --yes`)
- **`init` command** - Declining to reuse an existing bench now prompts for a different bench name and continues setup, instead of aborting the whole command

## [0.31.1] - 2026-06-24

### Fixed
- **`init` command** - Frappe `version-14` compatibility
  - Uses `--no-mariadb-socket` for `bench new-site` (matching `version-13`), replacing `--mariadb-user-host-login-scope=%` which is only supported in bench/Frappe 15+

## [0.31.0] - 2026-03-08

### Changed
- **`init` command** - Removed automatic default site setting after init
  - `bench use <site>` is no longer called after site creation
  - `bench set-config developer_mode 1` now explicitly targets the site with `--site <site_name>` instead of relying on a default
  - `bench set-config -g server_script_enabled 1` is unaffected (global config)

## [0.30.0] - 2026-03-06

### Fixed
- **`init` command** - `version-13` compatibility fixes
  - Uses `--no-mariadb-socket` for `bench new-site` (replaces `--mariadb-user-host-login-scope=%` which is not supported in older bench versions)
  - Pins `setuptools<82` immediately after `bench init` to retain `pkg_resources` compatibility
  - Installs `yarn` globally (`npm install -g yarn`) after the correct Node.js version is activated via nvm

### Improved
- **`init` command** - Clearer error message when the container runs out of disk space (`ENOSPC`), prompting the user to free up space rather than showing a generic exit-code error



### Changed
- **`init` command** - Bench image resolved from Docker Hub at runtime
  - Queries Docker Hub API for the latest stable semver tag of `frappe/bench` (e.g. `v5.29.1`)
  - Never uses the `:latest` tag — always pins to a specific version for reproducible builds
  - Falls back to a known-good version (`v5.29.1`) if the API is unreachable
  - Applies to all branches, not just `version-15`
  - Verbose mode logs the resolved tag and Docker Hub lookup progress

- **`init` command** - Automatic Python version pinning per Frappe branch
  - `version-15`: Uses `PYENV_VERSION=3.12.x`
  - `version-14`: Uses `PYENV_VERSION=3.10.x`
  - `version-13`: Uses `PYENV_VERSION=3.9.x`
  - Checks `~/.pyenv/versions` inside the container for installed versions
  - Automatically installs the correct Python version via `pyenv install` if not found
  - Verbose mode logs each step (discovery, installation, version used)

- **`init` command** - Automatic Node.js version pinning per Frappe branch
  - `version-14`: Uses Node.js 16 via `nvm use`
  - `version-13`: Uses Node.js 14 via `nvm use`
  - Checks `~/.nvm/versions/node/` inside the container for installed versions
  - Automatically installs the correct Node.js version via `nvm install` if not found
  - Verbose mode logs each step (discovery, installation, version used)

## [0.28.0] - 2026-01-29

### Added
- **`where` command** - Search all cached instances for apps or sites by name
  - Case-insensitive partial string matching across all projects
  - `--apps/-a` flag to search only for apps
  - `--sites/-s` flag to search only for sites
  - `--installed/-i` flag to show only installed apps (excludes available-but-not-installed)
  - `--json` flag for JSON output (useful for scripting)
  - Displays results in formatted tables with project, app/site name, version, and branch
  - Deduplicates results (prefers installed apps over available apps when both exist)
  - Examples: `cwcli where erpnext`, `cwcli where payments --apps`, `cwcli where .local --sites`

## [0.27.0] - 2026-01-21

### Added
- **`rm` command** - Remove (delete) Frappe projects with automatic backups
  - **Re-caches project before removal** to get accurate site information
  - **Creates database backups for all sites** before removal
  - **Archives project configuration** to `~/.cwcli/archive/{project_name}_{timestamp}/`
    - Backs up all site databases (with files)
    - Archives docker-compose.yml
    - Archives site_config.json for all sites
    - Includes archive metadata (project name, timestamp, bench path)
  - Stops and removes all containers for a project
  - **Removes Docker volumes by default** (complete removal)
  - Requires user confirmation before proceeding (destructive action)
  - `--no-volumes` flag to preserve volumes and keep data
  - `--no-backup` flag to skip backups and recaching (not recommended)
  - `--yes/-y` flag to skip confirmation prompt
  - Supports multiple projects via arguments or piped input
  - Clears project cache after removal
  - Verbose mode shows detailed removal, backup, and archive progress
  - Clear warning messages for data-destructive operations
  - Examples: `cwcli rm my-project`, `cwcli rm my-project --no-volumes`

## [0.26.0] - 2026-01-20

### Added
- **`restore` command** - Missing app detection before restore
  - Checks if apps from backup site are available on the target bench
  - Re-caches project before check to ensure accuracy
  - Displays warning with list of missing apps
  - Prompts for confirmation before proceeding with potentially incomplete restore
  - Provides helpful install instructions for missing apps
  - Applies to both local and remote (--receive) restore modes
  - New `--no-recache` flag to skip re-caching and use existing cache
- **`update` command** - Re-cache after app updates
  - Automatically re-caches project after apps are updated via git pull
  - Ensures site detection uses fresh cache data before migrations
  - Improves accuracy when finding which sites have updated apps installed
  - Verbose mode shows re-cache progress
  - New `--no-recache` flag to skip re-caching and use existing cache
- **`utils.cache.recache_project()`** - New utility function
  - Clears cache and re-runs inspect for a project
  - Ensures fresh, trustworthy cache data
  - Reusable for any future operations requiring up-to-date cache
  - Located in utils/cache.py for cache management operations

## [0.25.2] - 2026-01-20

### Fixed
- **`restore` command** - Auto-inspect when no cache found
  - Automatically runs inspect if no cached bench path or default site is found
  - Matches behavior of other commands (open, update, unlock)
  - Eliminates manual `cwcli inspect` step before restore
- **`restore` command** - Remote restore always available
  - "Restore from remote source (via sendme)" option now shown even when no local backups exist
  - Displays helpful "No local backups found" separator when applicable
  - Allows restoring from P2P transfers without requiring local backups first

## [0.25.1] - 2026-01-20

### Fixed
- **`restore --receive` command** - Encryption key update actually works now
  - Fixed heredoc implementation to properly write JSON without mangling
  - Previously reported success but failed to update encryption_key in site_config.json
  - Now matches the working implementation in main restore command

## [0.25.0] - 2026-01-08

### Added
- **`open` command** - Cursor editor support
  - New `--cursor` flag to open directly in Cursor (skips interactive prompt)
  - Cursor automatically detected and shown in interactive editor selection menu
  - Uses same Dev Containers integration as VS Code
  - Installation validation with helpful error messages pointing to https://cursor.sh/
  - Dynamic editor name display in status messages

## [0.24.0] - 2026-01-08

### Fixed
- **`init` command** - Frappe Docker image selection based on branch
  - Only pins Frappe bench image to `v5.26.0` for `version-15` branch to ensure stability
  - Uses latest image tag for `develop` branch to get the most current features
  - Prevents compatibility issues between branch versions and Docker images

## [0.23.1] - 2026-01-06

### Fixed
- **`restore --receive` command** - Resilient ticket input handling
  - Automatically strips all whitespace (newlines, tabs, spaces) from pasted sendme tickets
  - Handles janky copy-paste from terminal output with carriage returns and formatting
  - Users can now copy the entire output without carefully selecting just the ticket text

## [0.23.0] - 2026-01-05

### Added
- **`update` command** - Automatic maintenance mode management (opt-out)
  - Sites automatically enter maintenance mode before migrations to prevent user access during updates
  - Maintenance mode only enabled for affected sites (not entire bench)
  - Guaranteed cleanup via try-finally error handling - sites never stuck in maintenance mode even on failures
  - `--skip-maintenance` flag to disable maintenance mode if needed for operational hours
  - Per-site status messages in verbose mode matching other command output styles

### Changed
- **`update` command** - Performance optimization with database cache integration
  - Now uses `db_utils.get_cached_project_data()` for site-app lookups instead of repeated `bench list-apps` calls
  - O(n) performance with cache vs O(n×m) without cache
  - Graceful fallback to live queries if cache unavailable
  - Cache hit/miss logging in verbose mode
  - Recommendation: Run `cwcli inspect <project>` before `cwcli update` for best performance

### Fixed
- **`update` command** - Robust error handling prevents sites from being stuck in maintenance mode
  - Try-finally block ensures maintenance mode cleanup even on migration failures
  - Build failures, cache clearing failures, or exceptions no longer leave sites in maintenance mode
  - User interrupts (Ctrl+C) properly trigger maintenance mode cleanup
  - Inner try-except in finally block prevents cleanup errors from masking original errors
  - Consistent post-migration lock release delay (0.5s) in both verbose and non-verbose modes

## [0.22.0] - 2026-01-05

### Changed
- **`init` command** - Updated Frappe bench image tag from `latest` to `v5.26.0` for more predictable and stable deployments
  - Ensures consistent container behavior across different initialization times
  - Prevents unexpected changes from automatic image updates

## [0.21.1] - 2024-12-04

### Fixed
- **Auto-inspect service** - Now runs in update mode to fetch fresh data from running containers instead of only refreshing from cached data

## [0.21.0] - 2025-11-23

### Added
- **`init` command** - Initialize a complete Frappe development environment in a single step
  - Creates project directory structure in `~/.cwcli/projects/{project_name}/`
  - Downloads docker-compose.yml from frappe_docker GitHub repository
  - Pulls Docker images and starts containers automatically
  - Initializes Frappe bench inside the container
  - Creates a new site with configurable credentials
  - Optionally installs ERPNext with `--install-erpnext` flag
  - Custom port selection with `--port/-P` flag (creates ports {port}-{port+5} for web, {port+1000}-{port+1005} for socketio)
  - Port conflict detection before starting containers
  - Interactive prompts if project name not provided
  - Configurable Frappe/ERPNext branches with `--frappe-branch` and `--erpnext-branch`
  - Progress feedback with TipSpinner during long-running operations
  - Automatic bench path registration for `cwcli open` compatibility
  - Reuse existing bench with confirmation prompt
  - Elapsed time display on completion
  - Usage: `cwcli init <project_name> [--port 8000] [--install-erpnext]`

## [0.20.0] - 2025-11-16

### Changed
- **Home directory location** - Moved from `~/caffeinated-whale-cli/` to `~/.cwcli/` (hidden directory)
  - Config files now in `~/.cwcli/config/`
  - Cache database now in `~/.cwcli/cache/`
  - Auto-inspect PID/logs now in `~/.cwcli/run/`
  - Service names updated: `com.cwcli.auto-inspect` (macOS), `cwcli-auto-inspect.service` (Linux)
  - Log files updated: `/tmp/cwcli-auto-inspect.log` (macOS/Linux)
  - Follows standard Unix convention for hidden config directories

### Fixed
- Type annotations in completion utilities (added `| None` to optional parameters)

## [0.19.0] - 2025-01-15

### Added
- **P2P Backup Transfer via sendme** - Share and receive backups between machines using peer-to-peer connections
  - `cwcli restore <project> --send` - Share backup with remote machine via sendme
    - Interactive backup selection menu
    - Automatic sendme binary installation and management
    - Ticket automatically copied to clipboard for easy sharing
    - Multi-file transfer support (database, public files, private files, config)
    - Cross-platform support (macOS, Linux, Windows)
  - `cwcli restore <project> --receive` - Receive backup from remote machine
    - Ticket input prompt for receiving transfers
    - Automatic file download and verification (BLAKE3 hash-verified)
    - Files automatically copied to container's backup directory
    - Seamless integration with standard restore process
  - New `sendme_utils.py` module for sendme binary management
    - Platform detection (darwin-aarch64, darwin-x86_64, linux-x86_64, windows-x86_64)
    - Automatic binary download with progress bars
    - PATH configuration for Unix and Windows
    - Clipboard integration (pbcopy, xclip, xsel, clip)
  - Hash-verified transfers using BLAKE3 for data integrity
  - NAT traversal with automatic relay fallback
  - Resumable transfers (interrupted downloads can resume)
  - Support for multiple simultaneous receivers from one ticket

### Changed
- **Enhanced restore command** - Added `--send` and `--receive` modes for P2P transfers
- **Improved restore reliability** - Added `--force` flag to bypass Frappe version check prompts in non-interactive mode
- **Fixed file path handling** - File archives now use absolute paths for reliable restore operations

### Fixed
- **Terminal formatting issues** - Resolved escape code conflicts from sendme output
  - Added cursor position resets before interactive prompts
  - Isolated sendme process with `start_new_session=True` to prevent terminal state corruption
  - Simplified output to show only ticket copy confirmation instead of full ticket string
- **File archive paths** - Fixed "Invalid path" error by using full paths for `--with-public-files` and `--with-private-files`
- **Version mismatch prompts** - Added `--force` flag to restore command to handle version differences non-interactively

### Documentation
- Added comprehensive sendme CLI reference (`docs/technical/sendme-cli-reference.md`)
- Added Iroh blobs protocol security explanation (`docs/technical/iroh-blobs-and-sendme.md`)
- Updated Frappe backup/restore reference with real-world examples

## [0.15.0] - 2025-11-12

### Added
- **`restore` command** - Interactive site restoration from backups
  - Scans all backup files across all sites in the bench
  - Interactive backup selection menu with styled UI
  - Backups grouped by target site (shown first) and other sites
  - Automatic detection of file archives (public and private files)
  - Visual badges showing backup contents: `[FILES]`, `[PRIVATE]`, `[DATABASE ONLY]`
  - Automatic encryption key restoration from backup site_config
  - Secure password prompts using questionary library
  - TipSpinner integration for enhanced developer experience
  - Supports default site from common_site_config.json
  - Comprehensive input validation and error handling
  - Usage: `cwcli restore <project> [--site <site>]`

### Security
- **Command injection prevention in restore command**
  - Validates passwords don't contain single quotes (shell breaking)
  - Validates MariaDB username for shell metacharacters
  - Validates backup file paths before restore
  - Verifies backup files exist before attempting restore
  - Site name and bench path validation (prevents traversal attacks)
  - Password masking in verbose output

### Changed
- **Enhanced error messages for restore failures**
  - Lists common failure causes (incorrect password, connection issues, etc.)
  - Reminds users to use `-v` flag for detailed diagnostics
  - Shows restore output on failure for better debugging
  - JSON parsing errors handled gracefully with descriptive messages

## [0.14.0] - 2025-11-10

### Added
- **Site configuration caching** - Inspect command now caches site and bench configurations
  - `common_site_config.json` cached for each bench (includes Redis URLs, ports, worker settings)
  - `site_config.json` cached for each site (includes database credentials, developer mode)
  - New database tables: `common_site_config` and `site_config`
  - Helper functions to retrieve cached configs: `get_common_site_config()`, `get_site_config()`, `get_all_site_configs()`, `get_default_site()`
  - Configs are automatically fetched and stored during `cwcli inspect`
  - JSON output includes configs for programmatic access
  - Default site labeled with `(default)` in inspect output
  - Empty configs `{}` now properly preserved (distinguishes from missing configs)
- **Default site support** - `--site` flag now optional when default site is configured
  - `unlock` command automatically uses default site from `common_site_config.json`
  - Shows "Using default site: {site}" when using default
  - Helpful error messages when no default site available
  - Backward compatible: explicit `--site` flag still works

### Security
- **Filesystem permissions for sensitive cache data**
  - Cache directory created with restrictive permissions (`0700` - owner-only access)
  - Database file secured with `0600` permissions (owner read/write only)
  - Prevents unauthorized access to cached credentials and API keys
  - Security warnings added to model documentation
  - Comprehensive test suite validates permission enforcement
  - Note: Data is stored in plaintext; future enhancement will add field-level encryption
- **Command injection prevention in unlock command**
  - Input validation for site names and bench paths
  - Rejects shell metacharacters (`;`, `&`, `|`, `$`, etc.)
  - Prevents path traversal and injection attacks
  - Clear error messages for security violations

### Changed
- **Config validation improvements**
  - Empty dicts `{}` now accepted as valid configurations
  - Removed redundant JSON re-parsing in validation
  - Simplified storage checks to use `is not None` instead of truthiness
  - Better distinction between "no config" (None) and "empty config" ({})
- **Enhanced error handling in unlock command**
  - Try/except with proper exception chaining for default site retrieval
  - Validates site names are not empty or whitespace-only
  - Actionable error messages with tips for resolution

### Fixed
- **Config retrieval robustness**
  - `get_common_site_config()` now iterates all benches instead of just first
  - Returns config from first bench that has one (not just first bench)
  - Preserves empty configs throughout entire data pipeline
- **Code consistency**
  - Refactored `_get_common_site_config()` and `_get_site_config()` to use `_run_command` helper
  - Centralized verbose logging and command execution
  - Fixed unused variable warning in unlock command

## [0.13.1] - 2025-11-10

- **Contextual tips during long-running operations** - Inspired by Claude Code's tip system
  - Rotating helpful tips displayed alongside spinners during operations like `inspect`, `update`, and `open`
  - Tips help users discover features and best practices while waiting
  - 40+ curated tips covering VS Code integration, tab completion, caching, port management, and more
  - Tips rotate every 4 seconds during long operations to show variety
  - New `cwcli config tips` command group to manage tip display
    - `enable` - Enable contextual tips (default)
    - `disable` - Disable tips for simpler status messages
    - `status` - Check current tips display setting
  - Configurable via `show_tips` setting in config.toml (default: true)
  - Tips integrated into:
    - `inspect` command during project inspection
    - `open` command when preparing VS Code integration
    - Extension installation and container verification steps
  - TipSpinner context manager supports reuse across multiple operations

## [0.12.1] - 2025-11-10

### Added
- **Editor selection flags for `open` command** - Skip interactive prompt with direct flags
  - `--code` - Open directly with VS Code (validates installation)
  - `--code-insiders` - Open directly with VS Code Insiders (validates installation)
  - `--docker` - Open directly with Docker exec
  - Mutual exclusivity validation ensures only one flag can be specified
  - Backward compatible: interactive prompt still appears when no flag is specified

### Fixed
- **Docker exec now respects working directory** - `open` command with `--docker` or Docker selection
  - Container shell now opens in bench directory instead of container default
  - Respects `--app` flag to open in specific app directory
  - Uses Docker's `-w` flag to set working directory on exec

### Changed
- Moved `exec_into_container` function from `vscode_utils.py` to `docker_utils.py` for better organization

## [0.11.0] - 2025-11-09

### Added
- **Automatic project inspection** - Background service to keep project cache fresh
  - New `cwcli config auto-inspect` command group with full management suite
  - `enable` - Enable auto-inspection with configurable interval (default: 1 hour, minimum: 60 seconds)
  - `disable` - Disable auto-inspection and stop background process
  - `start` - Start the background daemon process
  - `stop` - Stop the background daemon process
  - `restart` - Restart the background process
  - `status` - Show detailed status (enabled, interval, process state, PID, startup configuration)
  - `logs` - View recent background process logs with `--lines` option
  - `set-interval` - Change inspection interval (requires restart to apply)
  - Cross-platform daemon support: fork (Unix/Linux/macOS) and threading (Windows)
  - Automatic inspection of all running Frappe projects at configured intervals
  - Background logging to `~/caffeinated-whale-cli/run/auto-inspect.log`
  - PID tracking in `~/caffeinated-whale-cli/run/auto-inspect.pid`

- **Automatic startup on system boot/login** - Platform-specific system integration
  - `install-startup` - Install platform-specific startup configuration
  - `uninstall-startup` - Remove startup configuration
  - `--startup` flag for `enable` and `start` commands to enable startup in one step
  - **macOS**: LaunchAgent plist file (`~/Library/LaunchAgents/com.caffeinated-whale-cli.auto-inspect.plist`)
  - **Linux**: systemd user service (`~/.config/systemd/user/caffeinated-whale-cli-auto-inspect.service`)
  - **Windows**: Task Scheduler task ("CaffeinatedWhaleCliAutoInspect")
  - Startup status shown in `status` command

- **Configuration options** in `config.toml`:
  - `auto_inspect.enabled` - Enable/disable auto-inspection (default: false)
  - `auto_inspect.interval` - Inspection interval in seconds (default: 3600)
  - `auto_inspect.startup_enabled` - Track startup configuration state (default: false)

## [0.10.2] - 2025-11-08

### Fixed
- **Critical:** Tab completion function signatures for Typer compatibility
  - Added required parameters (ctx, args, incomplete) to all completion functions
  - Fixed TypeError when Typer calls completion callbacks
  - Removed `sparse=True` from Docker query that prevented label access
  - Fixed DockerException: "Label data is not available for sparse objects"

### Changed
- Corrected `_cache` type annotation from `Dict[str, Dict[str, Any]]` to `Dict[str, Tuple[float, List[str]]]`

## [0.10.0] - 2025-11-08

### Added
- **Tab completion support** for project names, apps, and sites across all shells (Bash, Zsh, Fish, PowerShell)
  - Context-aware completions for all commands that accept project names
  - App name completions for `--app` option (open, update commands)
  - Site name completions for `--site` option (unlock command)
  - Fast completion with 2-second TTL caching
  - Install with `cwcli --install-completion`
- **CI/CD workflows** with GitHub Actions
  - Lint workflow: Runs Black, Ruff, and mypy on all branches and PRs
  - Build workflow: Builds package and verifies version consistency on master branch
  - Release workflow: Publishes to PyPI and creates GitHub releases on version tags
  - All workflows use `ghcr.io/astral-sh/uv` Docker images for fast, reproducible builds
- **Comprehensive documentation** in `docs/` directory
  - Contributing guides: git workflow, commit messages, branch naming, code quality, chores, CI/CD
  - Testing guide with examples and best practices
  - Technical documentation for bench management
  - Reorganized into logical categories: contributing, testing, technical

### Changed
- Bumped version to 0.10.0
- Enhanced main README with expanded Contributing section and quick links
- Consolidated CI/CD documentation into minimal `docs/contributing/ci-cd.md` guide

## [0.9.1] - 2025-11-05

### Fixed
- **Critical:** Port conflict detection now properly handles mixed scenarios where ports are held by both Frappe projects and external processes
  - Previously, if port 8000 was owned by a Frappe project and port 8080 by Postgres, stopping the Frappe project would allow the start to proceed, causing Docker to fail on the Postgres port
  - Now splits ports into Frappe-owned vs non-Frappe-owned, handles Frappe conflicts first, then re-checks ALL ports to catch remaining external conflicts
  - Ensures all port conflicts are resolved before allowing container startup
- **Critical:** Pressing Ctrl+C during port conflict prompts now cancels the entire start operation instead of just skipping to the next project
  - Previously, `typer.Exit(code=0)` from KeyboardInterrupt was caught indiscriminately, allowing the loop to continue
  - Now checks exit code: code 0 (user cancellation) propagates to exit entire command, code 1 (port conflicts) skips only the current project
  - Provides proper user control to cancel batch operations

## [0.9.0] - 2025-11-05

### Added
- **Port conflict detection and resolution system**
  - New `utils/port_utils.py` module with comprehensive port management functions
  - `get_project_ports()`: Extract all host ports used by a project's containers
  - `find_project_using_ports()`: Identify which Frappe projects are using specific ports
  - `is_port_in_use()`: Socket-based port availability checking
  - `check_ports_in_use()`: Batch port checking with verbose output option
  - `get_ports_in_use_with_processes()`: Cross-platform process identification (Linux/macOS/Windows)
  - `format_port_list()`: Smart port range formatting (e.g., "8000-8005, 9000")
  - `report_port_conflicts()`: User-friendly conflict reporting
- **Interactive port conflict resolution in `start` command**
  - Automatically detects port conflicts before starting containers
  - Identifies ports used by other Frappe projects vs. external processes
  - Offers to automatically stop conflicting Frappe projects via interactive prompts
  - Groups ports by project/process for cleaner, more readable output
  - Provides actionable error messages for non-Frappe port conflicts
  - Uses `questionary` for user-friendly confirmation prompts
- Cross-platform process identification using platform-specific tools:
  - **Linux/macOS**: `lsof` + `ps` to identify PID and process name
  - **Windows**: `netstat` + `tasklist` for process information
  - Graceful fallbacks when tools are unavailable
  - 2-second timeout protection on all system commands

### Changed
- **Enhanced `start` command workflow**
  - Now performs port conflict checks before attempting to start containers
  - Interactive conflict resolution prevents cryptic Docker port binding errors
  - Shows which projects/processes are blocking required ports
  - Improved error messages with specific guidance for resolution
- **Refactored `commands/utils.py` for better separation of concerns**
  - Removed ~500 lines of port management code (moved to dedicated module)
  - Focused on container lifecycle management only
  - `ensure_containers_running()`: Simplified to check container status without port checks
  - `_start_containers_for_command()`: Now delegates port checks to `start` command
  - Added documentation clarifying when port checks are performed
- **Standardized imports across all command modules**
  - All commands now import from `utils/docker_utils` instead of `commands/utils`
  - Consistent use of new `port_utils` module where needed
  - Updated: `inspect.py`, `logs.py`, `open.py`, `restart.py`, `run.py`, `status.py`, `stop.py`, `unlock.py`, `update.py`
- Enhanced `utils/docker_utils.py` with additional container management utilities
- Improved `utils/vscode_utils.py` with better VS Code integration
- Updated `utils/console.py` to export `stderr_console` for error reporting

### Fixed
- Eliminated "port is already allocated" Docker errors through proactive detection
- Port conflict messages now show process information for better debugging
- Container startup failures due to port conflicts are now prevented, not just reported

## [0.8.0] - 2025-10-12

### Added
- `unlock` command: remove locks folder for a specified site to unlock it
  - `--site/-s` flag to specify site name (required)
  - `--path/-p` flag to specify bench path (uses cached path from inspect by default)
  - `--verbose/-v` flag for streaming rm command output
  - Automatically runs `inspect` if no cached bench path is found
  - Uses `rm -rfv` in verbose mode to show files being removed
  - Helps resolve "document is currently locked" errors

### Changed
- **BREAKING**: `update` command now automatically clears locks for all affected sites after completion
  - Prevents stale locks from causing DocumentLockedError on subsequent runs
  - Clears locks after all operations complete (pull, migrate, build, cache clear)
  - Shows "Clearing locks: {site}" in progress spinner
- Enhanced `update` command reliability:
  - Added proper command completion waiting for streamed commands
  - Added polling loop to ensure ExitCode is available before proceeding
  - Added 0.5s delay after migrations to allow background processes to release locks
  - Increased Live display refresh rate to 20 per second for smoother animations
  - Made progress display transient (disappears after completion)
  - Fixed spinner freezing during blocking operations with manual `live.refresh()` calls

### Fixed
- Spinner animation now continues smoothly during all blocking Docker operations
- Commands properly wait for full completion before moving to next operation
- Lock files are automatically cleaned up, preventing migration failures

## [0.7.0] - 2025-10-10

### Added
- `update` command: update Frappe apps and migrate affected sites
  - `--app/-a` flag to specify apps to update (required, can specify multiple)
  - `--build/-b` flag to rebuild assets after updating apps
  - `--clear-cache/-c` flag to clear cache for all affected sites
  - `--clear-website-cache/-w` flag to clear website cache for all affected sites
  - `--path/-p` flag to specify bench path (uses cached path from inspect by default)
  - `--verbose/-v` flag for detailed output with streaming command execution
  - Automatically runs `inspect` if no cached bench path is found
  - Progress tracking with spinners in non-verbose mode
  - Comprehensive error tracking and reporting for all operations
  - Operations run in sequence: git pull → migrate → build → clear cache → clear website cache
  - Exits with error code 1 if any step fails, with detailed error summary

## [0.6.2] - 2025-10-10

### Changed
- Updated `.gitignore` to exclude `frappe_docker/` directory

## [0.6.1] - 2025-10-04

### Fixed
- `inspect` command now correctly displays available app names without requiring `--verbose` or `--show-apps` flags

## [0.6.0] - 2025-10-04

### Added
- `logs` command: view bench logs in real-time with `tail -f`
  - `--follow/-f` flag to follow logs (default: true)
  - `--no-follow` to show logs and exit
  - `--lines/-n` to specify number of lines to show (default: 100)
  - Logs stored in `/tmp/bench-{project_name}.log` inside container
- Shared console instances across all commands for consistent spinner behavior
  - Created `utils/console.py` with shared `console` and `stderr_console`
  - Fixes spinner artifacts and ensures verbose output is properly buffered

### Changed
- **BREAKING**: Replaced tmux session management with log file approach
  - `start` command now runs bench in background with nohup, logging to file
  - Simpler, more reliable - logs persist across laptop sleep/wake cycles
  - No more tmux dependencies or configuration needed
- Enhanced spinner UX for `start`, `stop`, and `restart` commands
  - Spinners now dynamically update to show which container is being started/stopped
  - Spinner shows bench startup status with log file location
  - All verbose output properly buffered until after spinner exits
- Improved output messages:
  - `start` and `restart` now show log file location and `cwcli logs` usage
  - Removed tmux-specific instructions

### Removed
- Tmux session management and configuration
- Tmux-related keybinding setup and config file creation

## [0.5.0] - 2025-10-03

### Added
- `open` command: `--app`/`-a` option to open a specific app directory within the bench
  - Verifies app exists in cached bench data
  - Shows available apps if requested app not found
  - Opens path at `{bench_path}/apps/{app_name}`

## [0.4.2] - 2025-10-03

### Fixed
- Windows compatibility: VS Code extension commands now work correctly on Windows by using platform-specific shell parameter

## [0.4.1] - 2025-10-03

### Changed
- `open` command now automatically runs `inspect` if no cached bench path is found
- Improved spinner behavior: single persistent spinner that exits before running nested commands
- Fixed spinner conflicts when `open` command calls `inspect` command

### Removed
- Unused `platformdirs` dependency

## [0.4.0] - 2025-10-03

### Added
- `open` command: open Frappe project instances in VS Code dev containers or Docker exec
  - Auto-detects VS Code and VS Code Insiders installations
  - Interactive editor selection with custom styled menu
  - Automatic installation of required VS Code extensions (Docker and Dev Containers)
  - Uses cached bench paths from `inspect` command
  - Spinners and verbose mode for progress feedback
  - Support for cancelling selection with Escape or Ctrl+C
- Enhanced UX with Rich spinners across multiple stages of operations
- Verbose mode (`-v`) shows all executed commands with `$ <command>` prefix

### Changed
- **BREAKING**: Removed bench alias system entirely
  - Removed `--bench`/`-b` global flag
  - Removed `config bench alias` and `config bench unalias` commands
  - Removed `list-apps` command
- **BREAKING**: Commands now use project names (from `cwcli ls`) instead of bench aliases
  - `run` command: `cwcli run <project_name> <bench_commands>`
  - `status` command: `cwcli status <project_name>`
  - `open` command: `cwcli open <project_name>`
- All commands now accept project name as first argument for consistency
- Updated help descriptions to be more concise and high-level
- `open` command automatically retrieves bench path from inspect cache

### Removed
- Bench alias database schema and all related functions
- `list-apps` command (functionality replaced by `inspect` with `--show-apps`)
- Global `--bench`/`-b` option

## [0.3.2] - 2025-08-03

### Added

- Interactive bench naming support for `cwcli inspect` (via `--interactive`, `-i`)
