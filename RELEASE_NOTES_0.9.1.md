# Caffeinated Whale CLI v0.9.1 Release Notes

**Release Date:** November 5, 2025
**Type:** Patch Release (Bug Fix)

## Overview

Version 0.9.1 is a critical patch release that fixes two bugs in the port conflict detection system introduced in v0.9.0:

1. **Mixed port conflicts** - Ensures all port conflicts are caught (Frappe + external processes)
2. **Ctrl+C handling** - Pressing Ctrl+C now properly cancels the entire operation instead of skipping to the next project

These fixes ensure proper port conflict resolution and user control over batch operations.

## Critical Bug Fix

### Mixed Port Conflict Detection

**Issue:** The port conflict resolution workflow could silently miss external process conflicts when Frappe project conflicts were also present.

**Scenario:**
- Project requires ports 8000 and 8080
- Port 8000 is held by Frappe project "alpha"
- Port 8080 is held by Postgres (external process)

**Previous Behavior (v0.9.0):**
1. User is prompted to stop Frappe project "alpha" ✓
2. Port 8000 is freed ✓
3. System returns success without checking port 8080 ✗
4. Docker Compose fails with cryptic "port is already allocated" error ✗

**New Behavior (v0.9.1):**
1. User is prompted to stop Frappe project "alpha" ✓
2. Port 8000 is freed ✓
3. **System re-checks ALL required ports** ✓
4. **Detects port 8080 still in use by Postgres** ✓
5. **Shows helpful error with process information** ✓
6. **Exits cleanly before Docker fails** ✓

### Ctrl+C Cancellation Handling

**Issue:** When starting multiple projects, pressing Ctrl+C during an interactive prompt would only skip the current project instead of canceling the entire operation.

**Scenario:**
- User runs `cwcli start project1 project2 project3`
- During the port conflict prompt for `project1`, user presses Ctrl+C
- User expects: Operation canceled, no projects started
- Actual (v0.9.0): Skip `project1`, continue with `project2` and `project3`

**Previous Behavior (v0.9.0):**
1. User presses Ctrl+C during prompt ✓
2. `typer.Exit(code=0)` is raised ✓
3. Caller catches ALL `typer.Exit` exceptions indiscriminately ✗
4. Loop continues to next project ✗
5. User cannot cancel batch operation ✗

**New Behavior (v0.9.1):**
1. User presses Ctrl+C during prompt ✓
2. `typer.Exit(code=0)` is raised ✓
3. **Caller checks exit code** ✓
4. **Exit code 0 (cancellation) propagates to cancel entire operation** ✓
5. **Exit code 1 (port conflict) skips only current project** ✓
6. **User has full control over batch operations** ✓

## Changes

### Fixed

- **Port Conflict Detection (Mixed Scenarios)** - `src/caffeinated_whale_cli/commands/start.py:20-212`
  - Split ports into Frappe-owned vs non-Frappe-owned sets
  - Handle Frappe project conflicts first with interactive prompts
  - Re-check ALL ports after stopping Frappe projects
  - Surface remaining external process conflicts with helpful error messages
  - Ensure all port conflicts are resolved before allowing container startup

- **Ctrl+C Cancellation Handling** - `src/caffeinated_whale_cli/commands/start.py:408-421`
  - Check exit code in exception handler: code 0 (user cancellation) vs code 1 (port conflict)
  - Propagate exit code 0 to cancel entire batch operation
  - Skip only current project for exit code 1
  - Restore expected Ctrl+C behavior for batch operations

### Technical Details

**Algorithm Improvements:**
1. **Port Classification** - Ports are now split into two sets:
   - Frappe-owned: Can be resolved by stopping Frappe projects
   - Non-Frappe-owned: Require manual intervention

2. **Two-Phase Resolution:**
   - **Phase 1:** Resolve Frappe conflicts interactively
   - **Phase 2:** Re-verify all ports and report remaining conflicts

3. **Enhanced Verbose Mode:**
   - Shows which ports are owned by Frappe projects
   - Shows which ports are owned by external processes
   - Confirms when all ports become available

**Error Messages:**
- Clear distinction between resolvable (Frappe) and non-resolvable (external) conflicts
- Process identification (PID and name) for external conflicts
- Actionable guidance for users

## Upgrade Notes

### Breaking Changes
None - this is a backward-compatible bug fix.

### Migration Guide
No migration needed. Simply update to v0.9.1:

```bash
pip install --upgrade caffeinated-whale-cli
```

### Recommended Actions
- If you experienced "port is already allocated" errors in v0.9.0, this release fixes that issue
- Use verbose mode (`-v`) to see detailed port conflict information during startup
- The `start` command will now catch ALL port conflicts before Docker attempts to bind them

## Testing

### Verified Scenarios

✅ **Pure Frappe Conflicts** - Multiple Frappe projects competing for ports
✅ **Pure External Conflicts** - External processes (Postgres, Redis, etc.) holding ports
✅ **Mixed Conflicts** - Combination of Frappe and external process conflicts
✅ **Sequential Resolution** - Stopping multiple Frappe projects one by one
✅ **User Cancellation (Decline)** - Graceful exit when user declines to stop conflicting projects
✅ **User Cancellation (Ctrl+C)** - Entire batch operation canceled when user presses Ctrl+C

### Test Cases

```bash
# Scenario 1: Mixed conflicts (fixed by this release)
# Port 8000: Frappe project "alpha"
# Port 8080: PostgreSQL
cwcli start my-project
# Expected: Prompts to stop "alpha", then shows Postgres conflict

# Scenario 2: Pure Frappe conflicts (still works)
# Ports 8000-8005: Frappe project "alpha"
cwcli start my-project
# Expected: Prompts to stop "alpha", proceeds on success

# Scenario 3: Pure external conflicts (still works)
# Port 8080: PostgreSQL
cwcli start my-project
# Expected: Shows error with process info, exits cleanly

# Scenario 4: Verbose mode
cwcli start my-project -v
# Expected: Shows detailed port ownership information

# Scenario 5: Ctrl+C cancellation (fixed by this release)
cwcli start project1 project2 project3
# Press Ctrl+C during port conflict prompt for project1
# Expected: Entire operation canceled, no projects started
# Previous (v0.9.0): Skips project1, continues with project2 and project3
```

## Impact

### Who Should Upgrade?

**High Priority:**
- Users who frequently run multiple Frappe projects
- Users who run other services (databases, web servers) on the same machine
- Users who experienced "port is already allocated" Docker errors in v0.9.0

**Medium Priority:**
- All users on v0.9.0 (this is a critical bug fix)
- Users automating project startup with scripts

**Low Priority:**
- Users running a single Frappe project with no other services
- Users who never experienced port conflicts

### Benefits

1. **Prevents Silent Failures** - No more cryptic Docker errors
2. **Complete Validation** - All port conflicts caught before Docker starts
3. **Better UX** - Helpful error messages with process information
4. **Time Savings** - Avoid debugging Docker errors
5. **Reliability** - Ensures clean startup or clear failure

## Known Issues

None reported.

## Future Plans

See [CHANGELOG.md](CHANGELOG.md) for planned features in upcoming releases.

## Links

- **GitHub Repository:** https://github.com/karotkriss/caffeinated-whale-cli
- **Issues:** https://github.com/karotkriss/caffeinated-whale-cli/issues
- **Documentation:** See [README.md](README.md)

## Credits

**Maintainer:** Christopher McKay
**License:** MIT

---

## Full Changelog: v0.9.0...v0.9.1

### Fixed (2)
1. **Critical:** Port conflict detection now properly handles mixed scenarios where ports are held by both Frappe projects and external processes
   - Splits ports into Frappe-owned vs non-Frappe-owned
   - Re-checks all ports after stopping Frappe projects
   - Surfaces remaining external conflicts before Docker startup

2. **Critical:** Pressing Ctrl+C during port conflict prompts now cancels the entire start operation instead of just skipping to the next project
   - Checks exit code to distinguish user cancellation (code 0) from port conflicts (code 1)
   - Propagates cancellation to exit entire batch operation
   - Restores expected Ctrl+C behavior

### Changed (0)
No changes.

### Added (0)
No new features.

### Removed (0)
Nothing removed.

---

**Install or Upgrade:**
```bash
pip install --upgrade caffeinated-whale-cli==0.9.1
```

**Verify Installation:**
```bash
cwcli --version
# Expected: Caffeinated Whale CLI Version: 0.9.1
```
