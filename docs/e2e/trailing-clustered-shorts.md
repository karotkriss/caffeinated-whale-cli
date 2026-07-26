# E2E: attached and clustered trailing short options

Real-instance validation for the fix that teaches the four variadic-project commands (`start`, `stop`, `restart`, `rm`) a proper short-option grammar, so `-pweb` means `-p web` and `-vy` means `-v -y`, while a cluster holding an unknown character is refused whole.

> **Status: the permanent proof is CI, not this run.**
> Real-instance validation for this fix belongs to [`tests/e2e/test_trailing_clustered_shorts_e2e.py`](../../tests/e2e/test_trailing_clustered_shorts_e2e.py), executed by the v14/v15/v16 matrix in `.github/workflows/e2e.yml` on an ephemeral runner, against the final pushed code, re-run on every fix commit, blocking the merge.
> That is strictly better than a local re-run: it validates the code that actually ships rather than the code the author happened to have checked out.
> **Do not copy the local procedure below as a recipe.** `docs/e2e/` is where canonical patterns get read from, and running a destructive lifecycle by hand on a shared box is no longer one of them - the committed test is.
> What follows is retained as the honest record of the run that first established this behaviour, and as the readable narrative of what the CI test asserts.
> The other half of the coverage is the unit tier, `tests/test_trailing_options.py`, which needs no instance at all.

- Instance: `fmclus38982`, a dedicated Frappe `version-16` instance built for this run with `cwcli init`, ports 16300-16305 / 17300-17305.
- Isolation: `CWCLI_HOME=/var/tmp/cwcli-home-fmclus38982`, binary is the worktree's own editable install (`.venv/bin/cwcli`).
- Date: 2026-07-25.
- Teardown: the instance and every resource created for it were removed by exact compose-project label, and the temp home deleted. No broad `docker prune`, no prefix-wide sweep, and no other instance was read from, modified, or removed at any point.

## 1. The defect, before the fix

These four commands are Typer sub-apps, hence Click **Groups**, and a Group sets `allow_interspersed_args = False`.
Every token written after the first project name is therefore dumped into the variadic project argument without ever reaching Click's parser, which is why these commands parse their own trailing options at all.
That splitter compared **whole tokens** against the option table, so forms Click accepts everywhere else were rejected:

```
$ cwcli restart nosuchproj -pweb
Error: No such option: -pweb
Run 'cwcli restart --help' to see the available options. Nothing was changed.
exit=2

$ cwcli start nosuchproj -vy
Error: No such option: -vy
Run 'cwcli start --help' to see the available options. Nothing was changed.
exit=2

$ cwcli rm nosuchproj -vy
Error: No such option: -vy
Run 'cwcli rm --help' to see the available options. Nothing was changed.
exit=2
```

## 2. The positive half, on a live instance

Asserted first, so nothing below can pass vacuously: the grammar has to be demonstrably live before "the malformed form did nothing" means anything.

```
=== 2. POSITIVE: valid clusters really drive the lifecycle ===
PASS  stop -v exited 0
PASS  stop -v really stopped the containers
PASS  start -vy exited 0
PASS  start -vy: the -v half of the cluster arrived
PASS  start -vy really started the containers
PASS  web is up (pid 187) before the attached-short restart
PASS  restart -pweb exited 0
PASS  restart -pweb: attached value reached --process
PASS  restart -pweb cycled ONLY web (187 -> 737)
```

`start -vy` is checked on both halves of the cluster, not just its exit code: `-y` is what carries consent, and `-v` is confirmed by start's verbose narration appearing on stderr, which only happens when `-v` genuinely arrived.
`restart -pweb` is confirmed by supervisord's own view - the `web` program came back on a **new pid** (187 -> 737), so the attached `web` really did reach `--process`.

## 3. The destructive half: no cluster may synthesise `-y`

A real `ToDo` was seeded through Frappe and read back (count=1) before any of this, so "the data survived" is a claim about data that was provably there.

Every form below was run against that live instance, through the real `rm` command path:

```
=== 4. ADVERSARIAL: no malformed cluster may reach the removal ===
PASS  -yq refused exit 2      PASS  -yq never reached the removal flow
PASS  -yq produced no confirmation                PASS  -yq deleted nothing
PASS  -qy refused exit 2      PASS  -qy never reached the removal flow
PASS  -qy produced no confirmation                PASS  -qy deleted nothing
PASS  -vyq refused exit 2     PASS  -vyq never reached the removal flow
PASS  -vyq produced no confirmation               PASS  -vyq deleted nothing
PASS  -y- refused exit 2      PASS  -y- never reached the removal flow
PASS  -y- produced no confirmation                PASS  -y- deleted nothing
PASS  -yb refused exit 2      PASS  -yb never reached the removal flow
PASS  -yb produced no confirmation                PASS  -yb deleted nothing
PASS  -y1 refused exit 2      PASS  -y1 never reached the removal flow
PASS  -y1 produced no confirmation                PASS  -y1 deleted nothing
PASS  -vq refused exit 2      PASS  -vq never reached the removal flow
PASS  -vq produced no confirmation                PASS  -vq deleted nothing
PASS  --yes-please refused exit 2
PASS  --yes-please never reached the removal flow
PASS  --yes-please produced no confirmation       PASS  --yes-please deleted nothing
PASS  the seeded record survived every refused form
```

"never reached the removal flow" is the absence of rm's `Preparing for removal...` line, which it prints the moment it begins preparing - ahead of the disclosure, the confirmation and the C1 backup gate.
"produced no confirmation" is the absence of `Are you sure you want to proceed?`.
"deleted nothing" re-derives the instance state independently of anything cwcli reported: a running frappe container, the named volume, the compose network, and the project directory all still present.

Six of the eight forms contain a literal `y`.
Had the grammar resolved clusters character by character and kept what matched, each would have become `cwcli rm fmclus38982 -y` - consent the user never spelled, on the most destructive verb in the tool.

The refusal text names the token the user actually typed:

```
$ cwcli rm demo -yq
Error: No such option: -yq
Run 'cwcli rm --help' to see the available options. Nothing was changed.
exit=2

$ cwcli rm demo -vyq
Error: No such option: -vyq
Run 'cwcli rm --help' to see the available options. Nothing was changed.
exit=2

$ cwcli rm demo -y-
Error: No such option: -y-
Run 'cwcli rm --help' to see the available options. Nothing was changed.
exit=2
```

An incomplete cluster - one ending in a value-taking short with no value to take - is a distinct usage error rather than a silent drop:

```
$ cwcli restart demo -vp
Error: Option '-p' requires a value.
exit=2
```

### Interactive mode

The same refusal driven through a real pty, where `isatty()` is true and a synthesised `-y` would surface as an actual confirmation prompt rather than a silent exit:

```
=== 5. ADVERSARIAL on a REAL pty (a synthesised -y would prompt here) ===
PASS  pty: exit 2
PASS  pty: no confirmation prompt
PASS  pty: never reached removal
PASS  pty refusal deleted nothing
```

## 4. The valid cluster really does delete

This is what makes section 3 non-vacuous: same command, same instance, one well-formed cluster.

```
=== 6. the VALID cluster really does delete (so step 4 was not vacuous) ===
PASS  rm -vy exited 0
PASS  containers gone
PASS  named volumes gone
PASS  network gone
PASS  project dir gone
```

`rm` was armed the entire time the eight refusals were being issued; the only difference is that `-vy` resolves completely and `-vyq` does not.

## 5. What did not change

- Unknown trailing **long** options are refused exactly as before (`--yes-please` above, and the `--benhc` case in [variadic-unknown-flag.md](variadic-unknown-flag.md)).
- A dash-leading value is still a value, not a cluster: an unknown short cluster is **returned as unresolved rather than raised**, so the positional rule still wins and `--bench -staging` / `--bench=-staging` keep resolving that label.
  The eager-help exception is a malformed cluster that reaches `-h` before an unknown short, such as `-vhq`; it is refused in either position after the complete token is validated.
  Widening the grammar to reject every leading dash would have broken every dash-leading bench label.
- Long-option behaviour, `--option=value` inline values, and valid `-h`/`--help` forms in trailing position are untouched.

## Note on the grammar's source of truth

The accepted forms are Click's own, not an approximation: a value-taking short swallows the rest of its token (`-pweb` = `--process web`) and stops the cluster, so `-pv web` is `--process v` with `web` still a project name, and `-p=web` yields the literal `=web` because Click does not strip `=` for short options.
`tests/test_trailing_options.py::TestGrammarMatchesClick` pins this by parsing the same argv through a real Click command and comparing, for both the accepted and the rejected forms, so the two cannot drift.
