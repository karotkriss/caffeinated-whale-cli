// While a View Transition runs, the page on screen is a snapshot: Chrome aims every press at
// <html>, so the element under the pointer never sees it, and an open dialog or popover takes
// it for a press outside itself and closes. No CSS lifts this (pointer-events on the
// transition's pseudo-elements changes nothing), so a press ends the transition and is handed
// to the live page instead: it is stopped at the window before anything else sees it, the
// transition is skipped, and once the new DOM is in and has had a frame, the press, its
// release and its click are dispatched again to the element under the pointer, focus moving
// with the press as a native one moves it. Only a primary press is handed over; any other
// press during the transition is stopped. Kept free of Vue and the DOM module graph so
// tests/test_webui_press_through.py can drive it under node with fakes.
const GESTURE = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']
const FOCUSABLE =
  'input, textarea, select, button, a[href], [contenteditable]:not([contenteditable="false"]), [tabindex]'

export function passPressesThrough(vt, doc = document) {
  const win = doc.defaultView
  let running = true
  let press = null // the gesture being handed over: its events so far, and its target once known
  const listen = (add) =>
    GESTURE.forEach((type) => win[add ? 'addEventListener' : 'removeEventListener'](type, handle, true))
  const end = () => {
    press = null
    if (!running) listen(false)
  }
  const deliver = (p, event) => {
    const handed = new event.constructor(event.type, event)
    p.target.dispatchEvent(handed)
    if (event.type === 'mousedown' && !handed.defaultPrevented)
      p.target.closest?.(FOCUSABLE)?.focus()
    if (event.type === 'click' && press === p) end()
  }
  function handle(event) {
    if (!event.isTrusted) return // a press dispatched here
    if (press && event.type === 'pointerdown') end() // the last press was released off the page
    if (!press && !running) return
    event.stopImmediatePropagation()
    if (event.type === 'mousedown') event.preventDefault() // focus moves with the handed press
    if (!press) {
      if (event.type !== 'pointerdown' || event.button !== 0) return
      const p = (press = { events: [], target: null })
      vt.skipTransition()
      vt.updateCallbackDone.catch(() => {}).then(() =>
        win.requestAnimationFrame(() => {
          if (press !== p) return
          const [down] = p.events
          p.target = doc.elementFromPoint(down.clientX, down.clientY) || doc.documentElement
          p.events.splice(0).forEach((e) => deliver(p, e))
        }),
      )
    }
    if (press.target) deliver(press, event)
    else press.events.push(event)
  }
  listen(true)
  const finish = () => {
    running = false
    if (!press) listen(false)
  }
  vt.finished.then(finish, finish)
}
