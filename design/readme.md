# Caffeinated Whale — design system

The brand and UI system for **Caffeinated Whale Desktop**, the Tauri desktop app being built on top of
**cwcli** (`caffeinated-whale-cli`) — a command-line tool for creating, managing and backing up local
Frappe/ERPNext Docker instances.

The CLI came first, and it is the reason this system looks the way it does. cwcli is a Python/Typer/Rich
program whose whole personality is *honest measurement*: it reports what it probed, when, and what it could
not find out. The desktop app is a face on that daemon, not a different product — so the design system
carries the CLI's vocabulary (its five health tokens, its Rich colour columns, its `help:` lines, its
rotating tips) into pixels rather than replacing them with generic dashboard chrome.

---

## Sources this system was built from

| Source | What was taken from it |
| --- | --- |
| **https://github.com/karotkriss/caffeinated-whale-cli** (branch `develop`) | Everything. Domain model, health tokens, action set, terminal colours, product copy and voice. |
| ↳ `src/caffeinated_whale_cli/core/fleet.py` | The fleet model — `InstanceState`, the INSTANT/FAST/LAZY tiers, and the rule that `unknown` is a state. |
| ↳ `src/caffeinated_whale_cli/core/status.py` | The four health tokens `running` / `online` / `degraded` / `offline`, plus `BenchStatus` and process fields. |
| ↳ `src/caffeinated_whale_cli/commands/serve.py` | The Console's endpoint surface and the Tier A safe action set. |
| ↳ `src/caffeinated_whale_cli/commands/list.py`, `where.py`, `config.py` | Rich column styles: project `cyan`, status `magenta`, ports `green`. |
| ↳ `src/caffeinated_whale_cli/utils/tips.py` | The TipSpinner and its curated tip strings. |
| ↳ `desktop/README.md`, `docs/e2e/console-ui.md` | The desktop shell's bring-up sequence, its branded error surface, and the master-detail + action-rail layout. |
| ↳ `.github/release-notes/v2.0.0.md`, `v2.1.0.md`, `skills/cwcli/SKILL.md` | Product voice — see **Content fundamentals** below. |
| **`uploads/cw.png`** (supplied by the user) | The brand mark, and every brand colour in `tokens/colors.css` (sampled from the file). |
| **https://github.com/lucide-icons/lucide** | 48 icon SVGs, vendored into `assets/icons/`. |
| **https://github.com/karotkriss/Caffeinated-Whale-Desktop** | Noted as the desktop repo; **not read** — the brief said to ignore all HTML in the repos. |

Reading those repositories directly will make anything you build here more accurate than working from this
document alone.

**Deliberately not used:** `src/caffeinated_whale_cli/commands/console.html`. The brief said to ignore all
HTML in the repo, so no visual decision here descends from it. Its *information architecture* — an instance
tree on the left, one detail pane in the centre, a narrow action rail on the right — is documented in prose
in `desktop/README.md` and `docs/e2e/console-ui.md`, and that documented IA is what the UI kit follows.

---

## The products

1. **cwcli** — the terminal program. Two surfaces in one binary: the human commands (Rich tables, spinners,
   prompts) and `cwcli axi`, the agent surface that prints TOON and never prompts.
2. **Caffeinated Whale Desktop** — a Tauri 2 shell that supervises one `cwcli serve` daemon on loopback and
   points its WebView at the Console. The shell owns bring-up, the window, and a branded error screen; the
   Python core owns every Docker and Frappe rule.

---

## Content fundamentals

**The voice is a competent engineer telling you what happened.** Declarative, specific, unhurried, never
excited. It is the same voice in the CLI, the release notes and the app.

- **Second person, present tense.** "Your multi-bench instances now report each bench separately." Not "We
  improved multi-bench reporting."
- **Lead with the fact, then the mechanism.** "A seventh bench used to bind its port inside the container and
  stay unreachable from your machine. `cwcli scale` widens the range of ports the instance publishes, and it
  leaves your database alone."
- **Sentence case everywhere.** Buttons: "Start instance", "Restart process", "Remove instance". The only
  capitals are 10px section labels and supervisord states (`RUNNING`, `STOPPED`, `BACKOFF`).
- **Machine words keep their exact spelling, in monospace, lowercase.** `running`, `degraded`, `unknown`,
  `worker_default`, `/workspace/development/frappe-bench`. Never retitle them to "Healthy" or "Default worker".
- **Every error names the fix.** The CLI's envelope is `error:` → context → `help: <the command to run>`.
  The `Banner` component reproduces that shape literally, `help:` prefix and all.
- **Admit what you do not know.** "No probe has answered for this instance yet." "never probed" is different
  from "no answer", and the UI says which. This is the single most characteristic thing about the product.
- **Name the consequence, don't shout it.** "This deletes the database. There is no undo." — not
  "⚠️ WARNING: DESTRUCTIVE ACTION".
- **Show the command.** Every empty state and every destructive dialog prints the equivalent `cwcli` line.
  The desktop app never pretends the CLI isn't there.
- **No marketing adjectives.** No "seamless", "powerful", "blazing". The release notes describe bugs plainly:
  "Long `cwcli init` phases stay visibly alive instead of looking hung."
- **Emoji: no.** One exception, inherited: the CLI prefixes its rotating tips with 💡. That character stays
  inside terminal output and never appears in app chrome.

---

## Visual foundations

**Colour.** Every brand value is sampled from the mark in `assets/logo-whale.png` — a breaching whale in a
coffee-coloured wave. Deep-navy outlines became the ink ramp (`--cw-ink-*`, `--cw-abyss`), the whale's body
became the single accent (`--cw-whale-500 #00a0d0`), the foam became the text whites (`--cw-foam-*`), and the
coffee cup became the one warm colour (`--cw-clay-*`, used for illustration only, never for UI state).
Neutrals are deliberately blue-cast — sea, not slate. Two colour systems sit on top: the five **health
tokens** (`--cw-status-*`) and the **terminal palette** (`--cw-term-*`) that mirrors what Rich prints.

**Theme.** Dark by default (`:root`), because the app lives next to a terminal. A light scope exists at
`[data-theme="light"]` for documentation and screenshots.

**Type.** IBM Plex Sans for chrome, JetBrains Mono for anything a machine produced. 13px body, 12px meta,
11/10px for labels — the UI is a fleet of instances, each with benches, each with processes, and density is
the point. Display sizes appear only in the splash, dialogs and empty states.

**Backgrounds.** Flat. No gradients in the app chrome; the splash uses one very low-contrast radial wash from
`--cw-ink-800` to `--bg-app` and that is the only gradient in the system. No photography, no illustration
except the mark itself, no patterns, no texture, no grain.

**Cards and surfaces.** A card is `--bg-surface`, a 1px `--border-subtle` hairline, `--radius-lg` (8px), and
`--shadow-sm`. Nothing else. Cards do not nest. Panes are separated by hairlines, not by gaps or shadows.

**Borders and shadows.** Depth is a hairline plus a wide, low-opacity black shadow — never a glow, never a
coloured drop shadow. The only coloured shadow in the system is the 2px focus ring.

**Corner radii.** 3 / 4 / 6 / 8 / 12px, plus a pill for status only. Controls are 6px. The window is 10px.

**Layout.** Fixed chrome: 38px title bar, 44px toolbar/tab strip, 264px fleet tree, 216px action rail, 26px
status bar, 28px rows, 30px controls. The centre pane is the only thing that flexes. Scrolling is per-pane;
the window never scrolls as a whole.

**Interaction states.** Hover is a translucent neutral tint (`--bg-hover`), never a colour change. Press is a
0.5px downward nudge — no scale, no shadow. Focus is a 2px cyan ring. Selection is a tinted row plus a 2px
accent bar down its left edge. Disabled is 42% opacity **and a reason**: every disabled action in the rail
carries the sentence explaining why.

**Motion.** 80/120/160/240ms on `cubic-bezier(.2,.6,.3,1)`. Nothing bounces, springs, or slides more than a
few pixels. Two looping animations exist in the whole system: the spinner, and the slow pulse on an
`unknown` health dot. A newly arrived delta gets one 900ms accent flash and then sits still.

**Transparency and blur.** Used twice: the modal scrim (`--bg-scrim`, 2px blur) and translucent hover tints.
Nothing else is frosted.

**Imagery.** There is exactly one image in the brand: the whale mark. It is full-colour, never recoloured,
never cropped, never placed on a busy background. If a surface needs a mark and the PNG is unavailable, set
the words "Caffeinated Whale" in IBM Plex Sans SemiBold instead.

---

## Iconography

- **Lucide** (https://lucide.dev, ISC), 48 glyphs, vendored as SVG into `assets/icons/` and exposed through
  the `Icon` component (`components/icons/Icon.jsx`), which inlines the path data so colour follows
  `currentColor`.
- **Substitution flagged:** cwcli ships no icon set of its own — it is a terminal program. Lucide was chosen
  because its 1.5px stroke and 24px grid match the density of this UI. If the desktop repo later adopts a
  different set, replace `assets/icons/` and regenerate `Icon.jsx` from it.
- **Sizes:** 14px in dense rows, 16px default, 18px in toolbars and dialog headers. Stroke is always 1.5.
- **Colour:** always `currentColor`, inherited from the row or button. Icons are never multi-colour and never
  carry a background.
- **Unicode as iconography:** the terminal surface uses box-drawing characters for Rich tables, `⠋⠙⠹…` for the
  dots spinner, `→` in event messages, and `—` for an unmeasured value. These belong to the terminal, not to
  app chrome.
- **Emoji:** not used in the app. The CLI's 💡 tip prefix is the sole inherited exception.
- **No hand-drawn SVG.** If a glyph is missing, add it from Lucide rather than drawing one.

---

## Index

```
readme.md          this file
SKILL.md           agent-skill front matter for use in Claude Code
github.md          upstream source association + screen map
styles.css         the one entry point consumers link (imports only)
thumbnail.html     homepage tile

tokens/            fonts, colors, typography, spacing, radius, elevation, motion, base
guidelines/        21 foundation specimen cards (Colors, Type, Spacing, Brand)
assets/            logo-whale.png + icons/ (48 Lucide SVGs)
components/        the reusable primitives, grouped by concern
ui_kits/desktop/   the Tauri Console window, click-through
ui_kits/cli/       the terminal surface: Rich table, TOON, typed error, tip spinner
```

### Components

Grouped by concern. Every component is a `.jsx` with a sibling `.d.ts` (props contract) and `.prompt.md`
(what & when), and each directory has one `@dsCard` HTML showing its states.

- **`components/core/`** — `Button`, `IconButton`, `Badge`, `Tag`, `Card`, `Kbd`
- **`components/status/`** — `StatusPill`, `HealthDot`, `ProcessRow`, `EventRow`
- **`components/forms/`** — `Input`, `SearchField`, `Select`, `Checkbox`, `Switch`
- **`components/navigation/`** — `TreeItem`, `Tabs`, `ActionRail`
- **`components/feedback/`** — `Banner`, `Dialog`, `Spinner`, `EmptyState`
- **`components/data/`** — `KeyValue`, `DataTable`, `LogView`
- **`components/icons/`** — `Icon` (plus `ICON_NAMES`, `ICON_PATHS`)

**Intentional additions.** cwcli is a terminal program and defines no React component library, so this
inventory was authored rather than transcribed. Each primitive exists because a surface documented in the
repo needs it: `StatusPill`/`HealthDot`/`ProcessRow`/`EventRow` render the fleet model's own fields,
`ActionRail` is the Tier A action set, `Banner` is the `CwcliError` envelope, `Spinner` is `TipSpinner`,
`LogView` is the bounded log tail, `DataTable` is a Rich table, and `Icon` wraps the vendored glyph set.
No component here exists only because a design system "usually" has one.

### UI kits

- **`ui_kits/desktop/index.html`** — the Console: fleet tree → bench detail (processes / logs / apps / sites),
  Tier A action rail, live SSE delta strip, init and remove dialogs. Clickable end to end.
- **`ui_kits/desktop/splash.html`** — the bring-up sequence the Rust shell owns, and its typed failure screen.
- **`ui_kits/cli/index.html`** — how cwcli prints: Rich table, TOON, typed error, tip spinner.

---

## Known gaps

- **Fonts are substitutions.** cwcli ships no webfonts. IBM Plex Sans + JetBrains Mono are loaded from Google
  Fonts in `tokens/fonts.css`. If the desktop app has licensed families, replace that `@import` with
  self-hosted `@font-face` rules.
- **No Mobbin references were used.** The brief asked for Mobbin picks as a starting point; that connector
  was not available in this session, so the desktop layout is grounded entirely in the repo's own documented
  IA. Send specific Mobbin screens and the kit can be re-cut against them.
- **Only Tier A actions are designed.** Restore, scale, app checkout and site removal exist in the CLI but
  have no designed desktop surface yet, because the shipped Console does not expose them.
