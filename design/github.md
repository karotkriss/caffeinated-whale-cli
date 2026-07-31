repo: karotkriss/caffeinated-whale-cli
branch: develop
path: src/caffeinated_whale_cli, desktop, docs, .github/release-notes

## Last sync

date: 2026-07-31T06:17:36Z

### Updated in this project

- Built the whole design system from the cwcli sources: tokens, 25 components, 3 screens.
- Health tokens, Rich column colours and the Tier A action set taken from `core/` and `commands/serve.py`.
- Product voice distilled from the v2.0.0 / v2.1.0 release notes and `skills/cwcli/SKILL.md`.
- 48 Lucide icons vendored into `assets/icons/` (cwcli ships none of its own).

## Screen map

| Screen | Built from |
| --- | --- |
| `ui_kits/desktop/index.html` (Console) | `desktop/README.md`, `docs/e2e/console-ui.md`, `commands/serve.py`, `core/fleet.py`, `core/status.py` |
| `ui_kits/desktop/splash.html` (bring-up) | `desktop/README.md` (bring-up sequence, error surface), `desktop/src-tauri/tauri.conf.json` |
| `ui_kits/cli/index.html` (terminal) | `commands/list.py`, `commands/where.py`, `utils/tips.py`, `skills/cwcli/SKILL.md` |
| `tokens/colors.css` (terminal + health) | `core/status.py`, `core/fleet.py`, `commands/list.py`, `core/rm.py` |
| `components/*` | `core/fleet.py`, `core/status.py`, `core/supervision.py`, `commands/serve.py` |
| `readme.md` — content fundamentals | `.github/release-notes/v2.0.0.md`, `v2.1.0.md`, `skills/cwcli/SKILL.md` |

Notes: `src/caffeinated_whale_cli/commands/console.html` was deliberately not read for visual reference —
the brief said to ignore all HTML in the repo. `karotkriss/Caffeinated-Whale-Desktop` was not read.
