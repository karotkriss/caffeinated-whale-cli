# E2E evidence: TOON help on the `cwcli axi` surface

Date: 2026-07-25

## Scope

This run reproduced and fixed decorated Rich help on the agent-facing surface.
It changed only `cwcli axi ... --help`.
The human `cwcli ... --help` surface kept its existing Rich output.

The real registry contained 23 top-level AXI commands:

```text
ls where backup unlock stop start status logs restart scale inspect benches
label migrate run-tests build config init rm rm-site self-update setup apps
```

The nested `apps` group contained four commands:

```text
list update checkout install
```

The complete regression walk therefore exercised 28 help paths: the AXI group, its 23 commands, and the four commands nested below `apps`.

`-h` was not an accepted help alias before this change.
It remains outside the help contract because adding an alias would change the parser surface.

## Reference tools

The captain named two installed tools as the reference.
Their current captured sizes differed slightly from the filing measurements:

| Command | Captured bytes |
| --- | ---: |
| `tasks-axi --help` | 794 |
| `no-mistakes axi --help` | 793 |

`tasks-axi` remained the shape reference: one usage line, counted command and flag arrays, then examples.

## Runtime-only installation

Both the before and after commands came from isolated, non-editable tool installations:

```bash
UV_TOOL_DIR=<isolated>/tool UV_TOOL_BIN_DIR=<isolated>/bin uv tool install .
uv pip list --python <isolated>/tool/caffeinated-whale-cli
```

The installed environment contained the 21 runtime packages resolved from the project metadata.
`pytest` was absent, which proved that development dependencies did not leak into the artifact under test.

The installed dependency set resolved Typer 0.27.0 and Click 8.4.2.
The development environment used Typer 0.16.0 and Click 8.2.1.
The first implementation passed the development tests but failed the installed-artifact positive checks because Typer 0.27 uses vendored Click command classes.
The final renderer recognizes command, argument, and option metadata through Click's stable command protocol and `param_type_name`, so both dependency shapes produce the same TOON schema.

## Measured output

| Help path | Before bytes | After bytes | Saved | Reduction |
| --- | ---: | ---: | ---: | ---: |
| `cwcli axi --help` | 5,153 | 470 | 4,683 | 90.9% |
| `cwcli axi scale --help` | 2,577 | 1,171 | 1,406 | 54.6% |
| `cwcli axi apps --help` | 2,012 | 330 | 1,682 | 83.6% |
| `cwcli axi apps install --help` | 6,153 | 3,799 | 2,354 | 38.3% |

The longer nested leaf still carries all seven safety and behavior notes from its command documentation.
Its reduction comes from removing Rich framing, wrapping, and alignment rather than deleting those details.

The installed group output became:

```toon
usage: "cwcli axi [command] [args] [flags]"
description: "Agent-facing surface: structured TOON output on stdout, no interactive prompts."
commands[23]: ls,where,backup,unlock,stop,start,status,logs,restart,scale,inspect,benches,label,migrate,run-tests,build,config,init,rm,rm-site,self-update,setup,apps
flags[1]{name,value,required,default,description}:
  "--help",boolean,false,null,"Show this message and exit."
examples[2]:
  cwcli axi
  cwcli axi <command> --help
```

The installed representative leaf output began:

```toon
usage: "cwcli axi scale <project> [flags]"
description: "Widen the instance's published port range; emit the new port map as TOON."
arguments[1]{name,value,required,description}:
  project,str,true,"The Docker Compose project name."
flags[3]{name,value,required,default,description}:
  "--to",int,false,null,"Ensure at least this many benches are host-reachable (publish at least this many ports). Omit to auto-fit every bench."
  "--yes/-y",boolean,false,false,"Consent to the whole-instance restart that expanding the port range causes."
  "--help",boolean,false,null,"Show this message and exit."
examples[2]:
  cwcli axi scale <project>
  cwcli axi scale <project> --to <int>
```

## Positive checks before negative checks

Each installed help path first had to prove all of the following:

- Exit code 0 and non-empty output.
- A real usage line containing the mounted `cwcli axi` path.
- A description.
- A counted flags table with value shapes, required state, defaults, and descriptions.
- A counted examples block.
- Every real argument name and description for leaf commands.
- Every real option spelling and description.
- Every real child command name for groups.

Only after those checks passed did the proof reject box-drawing characters, ANSI escape sequences, blank alignment lines, trailing spaces, non-TOON indentation, and repeated spaces used as column padding.
All 28 installed paths passed.

## Human surface proof

Two representative human help paths were captured before and after from the same runtime-only installation shape.
Both were byte-identical:

| Help path | Bytes | SHA-256 before and after |
| --- | ---: | --- |
| `cwcli --help` | 4,558 | `04a6144bbda09d47048e69be86cb456615fa204ae77a9af9c329b36e3fa80272` |
| `cwcli scale --help` | 2,735 | `528218b39f744424733d0ce8a73c3300b3ba632d97afd0b3766d3027ad3cf9ad` |

The human output still contained its Rich Arguments and Options panels.
