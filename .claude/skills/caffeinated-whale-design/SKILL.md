---
name: caffeinated-whale-design
description: Use this skill to generate well-branded interfaces and assets for Caffeinated Whale (the cwcli CLI and Caffeinated Whale Desktop), either for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping.
metadata:
  internal: true
---

The canonical Caffeinated Whale design system lives in this repo at `design/` (committed source of truth; do not duplicate it here).

Start by reading `design/readme.md`, then explore the rest of `design/`:

- `design/tokens/` - the CSS custom-property tokens (colors, type, spacing, radius, elevation, motion, fonts); `design/styles.css` imports them all.
- `design/components/` - 25 JSX components in 7 groups (core, data, feedback, forms, icons, navigation, status), each with a `.d.ts` and a `.prompt.md`; `design/components/*/**.card.html` are live preview cards.
- `design/guidelines/` - the guideline cards (colors, type, spacing, radius, elevation, motion, iconography, brand mark, states).
- `design/assets/` - the whale mark (`logo-whale.png`) plus 48 vendored Lucide icons under `assets/icons/`.
- `design/ui_kits/` - full-screen reference kits (`desktop/` Console + splash, `cli/` terminal).
- `design/_adherence.oxlintrc.json` - the adherence lint config (raw-hex/raw-px/font/component-prop rules); desktop frontend work is checked against it (see `desktop/README.md`).

If creating visual artifacts (slides, mocks, throwaway prototypes), copy assets out of `design/` and build static HTML for the user to view. If working on production code (the Tauri Console/shell), read the rules in `design/` to design with this brand and derive styling from its tokens.

If the user invokes this skill without other guidance, ask them what they want to build or design, ask a few questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.
