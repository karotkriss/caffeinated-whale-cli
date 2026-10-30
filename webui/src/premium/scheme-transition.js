// The switch between light and dark as a same-document View Transition: direction C
// (the soft diagonal wipe) from the theme-transition decision board. frappe-ui mutes
// every element's own colour transition for the swap (55 of them on the Instances
// page would otherwise flash), so a transition cannot be "let the CSS play": the
// browser snapshots the whole page before and after the swap and blends the two
// images on the compositor. Kept free of Vue and the DOM module graph so
// tests/test_webui_scheme_transition.py can drive it under node with a fake document.
export const WIPE_MS = 560 // the captain's pick on the board
export const EASE = 'cubic-bezier(0.23, 1, 0.32, 1)' // --ease-out (webui/src/style.css)

// Which blend plays: the wipe only when a control was pressed, effects are on and the
// OS does not ask for less motion; otherwise the 200 ms cross-fade (reduced motion,
// visual effects off, an OS-driven change under `system`, where nothing was pressed).
export function pickDirection({ reduced, enabled, origin }) {
  return reduced || !enabled || !origin ? 'fade' : 'wipe'
}

// Run `apply` (the scheme swap) inside a view transition and return which blend played.
// Without the API the swap is instant, as before. `data-vt` on <html> selects the
// pseudo-element rules in premium.css; it is deliberately never cleared afterwards:
// a press mid-flight starts a new transition that must still match it.
export function transitionScheme(apply, choice, doc = document) {
  if (typeof doc.startViewTransition !== 'function') {
    apply()
    return 'instant'
  }
  const dir = pickDirection(choice)
  const root = doc.documentElement
  root.dataset.vt = dir
  const vt = doc.startViewTransition(apply)
  if (dir === 'wipe') {
    // The new snapshot sweeps in under the feathered 105deg mask (premium.css) while the old one holds still.
    vt.ready
      .then(() => root.animate(
        { maskPosition: ['100% 0', '0 0'], webkitMaskPosition: ['100% 0', '0 0'] },
        { duration: WIPE_MS, easing: EASE, pseudoElement: '::view-transition-new(root)' },
      ))
      .catch(() => {}) // a transition skipped by a newer one rejects `ready`; nothing to undo
  }
  return dir
}
