// The premium layer's one store: effects on/off, the gradient theme, the colour
// scheme, and the two environmental gates (prefers-reduced-motion, hidden tab).
// `motion` is the only flag the shader and motion-v consumers read: it is true
// only when effects are on AND the OS does not ask for reduced motion AND the
// tab is visible. Persisted in localStorage next to frappe-ui's own `theme` key.
import { computed, reactive, readonly, ref, watch, watchEffect } from 'vue'
import { useColorScheme } from 'frappe-ui'
import { DEFAULT_THEME, THEMES, themeById } from './themes'
import { transitionScheme } from './scheme-transition'

const KEY = 'cw.premium'
const stored = (() => { try { return JSON.parse(localStorage.getItem(KEY) || '{}') } catch { return {} } })()
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')

export const effects = reactive({
  enabled: stored.enabled ?? true, // the settings toggle
  theme: THEMES.some((t) => t.id === stored.theme) ? stored.theme : DEFAULT_THEME,
  reducedMotion: reduced.matches,
  hidden: document.visibilityState === 'hidden',
})
reduced.addEventListener('change', (e) => (effects.reducedMotion = e.matches))
document.addEventListener('visibilitychange', () => (effects.hidden = document.visibilityState === 'hidden'))
watch(() => [effects.enabled, effects.theme], ([enabled, theme]) => localStorage.setItem(KEY, JSON.stringify({ enabled, theme })))

const { colorScheme, resolvedColorScheme, setColorScheme } = useColorScheme()
export const scheme = colorScheme
// The scheme the page is PAINTED in. It follows frappe-ui's resolved scheme, but only from
// inside a transition's swap, so the gradient and the charts (which read it) change in the
// new snapshot and never a frame early.
const painted = ref(resolvedColorScheme.value)
export const resolvedScheme = readonly(painted)
const osDark = window.matchMedia('(prefers-color-scheme: dark)')
const swap = (id) => { setColorScheme(id); painted.value = resolvedColorScheme.value }
const choice = (origin) => ({ reduced: effects.reducedMotion, enabled: effects.enabled, origin })
// Change the preference, blending the page from the old scheme to the new one (see
// scheme-transition.js). `origin` is the control that was pressed, if any: the wipe plays
// only for a press. A change that leaves the painted scheme as it is (system -> light on
// a light OS) only stores the preference.
export function setScheme(id, origin = null) {
  const next = id === 'system' ? (osDark.matches ? 'dark' : 'light') : id
  if (next === painted.value) return swap(id)
  transitionScheme(() => swap(id), choice(origin))
}
// An OS switch under `system`: frappe-ui's own listener swaps the attribute at once (the
// instant change this page used to make). By this frame's animation-frame callbacks it has
// done so and nothing has painted yet, so the old value goes back on and the cross-fade
// carries the change instead.
osDark.addEventListener('change', () => {
  if (scheme.value !== 'system') return
  requestAnimationFrame(() => {
    if (resolvedColorScheme.value === painted.value) return
    document.documentElement.setAttribute('data-theme', painted.value)
    transitionScheme(() => swap('system'), choice(null))
  })
})

export const currentTheme = computed(() => themeById(effects.theme))
export const stops = computed(() => currentTheme.value[resolvedScheme.value])
// Effects drawn at all (static when motion is off); motion = the shader may move.
export const active = computed(() => effects.enabled)
export const motion = computed(() => effects.enabled && !effects.reducedMotion && !effects.hidden)

watchEffect(() => {
  document.documentElement.toggleAttribute('data-effects', effects.enabled)
})
