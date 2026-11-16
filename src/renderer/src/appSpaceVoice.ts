/** Routes a held Space gesture to one eligible voice surface in the app window. */
export const APP_SPACE_VOICE_PRIORITY = { general: 0, focused: 10, exclusive: 20 } as const

type SpaceVoiceBinding = {
  priority: number
  eligible: () => boolean
  begin: () => boolean
  finish: () => void
  cancel: () => void
}

const bindings = new Set<SpaceVoiceBinding>()
let held: SpaceVoiceBinding | null = null
let consumedDown = false
let blurredDuringHold = false
let listening = false

const interactive = [
  'button, input, textarea, select, a[href], summary, audio[controls], video[controls]',
  '[contenteditable]:not([contenteditable="false"]), [role="dialog"], [tabindex]:not([tabindex="-1"])',
  '[role="textbox"], [role="searchbox"], [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"]',
  '[role="tab"], [role="option"], [role="menuitem"], [role="combobox"], [role="listbox"], [role="slider"], [role="spinbutton"]',
].join(', ')

function isSpace(event: KeyboardEvent): boolean {
  return event.code === 'Space' || event.key === ' '
}

function allowsTalk(event: Event): boolean {
  return !event.composedPath().some(node => node instanceof Element && node.closest(interactive))
    && !document.activeElement?.closest(interactive)
}

function winner(): SpaceVoiceBinding | undefined {
  if (document.hidden) return undefined
  const ordered = [...bindings].reverse().sort((first, second) => second.priority - first.priority)
  try {
    return ordered.find(binding => binding.eligible())
  } catch {
    // A broken eligibility check must not send the gesture to another surface.
    return undefined
  }
}

function cancel(binding: SpaceVoiceBinding): void {
  try { binding.cancel() } catch { /* Still retire the gesture if this surface is tearing down. */ }
}

function cancelHeld(): void {
  if (!held) return
  const binding = held
  held = null
  cancel(binding)
}

function refresh(): void {
  if (held && winner() !== held) cancelHeld()
}

function keyDown(event: KeyboardEvent): void {
  if (!isSpace(event) || event.defaultPrevented) return
  if (blurredDuringHold && !event.repeat) {
    // The previous release may have happened while the app was out of focus.
    consumedDown = false
    blurredDuringHold = false
  }
  if (held || consumedDown) {
    event.preventDefault()
    return
  }
  if (event.repeat || event.isComposing || event.key === 'Process' || event.altKey || event.ctrlKey || event.metaKey
    || event.shiftKey || !allowsTalk(event)) return

  const binding = winner()
  if (!binding) return
  try {
    if (!binding.begin()) return
  } catch {
    cancel(binding)
    return
  }
  consumedDown = true
  event.preventDefault()
  if (winner() === binding && allowsTalk(event)) held = binding
  else cancel(binding)
}

function keyUp(event: KeyboardEvent): void {
  if (!isSpace(event)) return
  if (consumedDown) event.preventDefault()
  consumedDown = false
  blurredDuringHold = false
  if (!held) return
  const binding = held
  held = null
  if (winner() !== binding || !allowsTalk(event)) {
    cancel(binding)
    return
  }
  try { binding.finish() } catch { cancel(binding) }
}

function focusChanged(event: FocusEvent): void {
  if (held && !allowsTalk(event)) cancelHeld()
}

function leftAppWindow(): void {
  blurredDuringHold = consumedDown
  cancelHeld()
}

function visibilityChanged(): void {
  if (document.hidden) leftAppWindow()
}

function startListening(): void {
  if (listening) return
  listening = true
  window.addEventListener('keydown', keyDown)
  window.addEventListener('keyup', keyUp)
  window.addEventListener('blur', leftAppWindow)
  window.addEventListener('focusin', focusChanged)
  window.addEventListener('hsc-privacy-pause', cancelHeld)
  document.addEventListener('visibilitychange', visibilityChanged)
}

function stopListening(): void {
  if (!listening) return
  listening = false
  consumedDown = false
  blurredDuringHold = false
  window.removeEventListener('keydown', keyDown)
  window.removeEventListener('keyup', keyUp)
  window.removeEventListener('blur', leftAppWindow)
  window.removeEventListener('focusin', focusChanged)
  window.removeEventListener('hsc-privacy-pause', cancelHeld)
  document.removeEventListener('visibilitychange', visibilityChanged)
}

/** Surfaces retain their own permission checks and microphone implementation. */
export function registerAppSpaceVoice(binding: SpaceVoiceBinding): {
  unregister: () => void
  refresh: () => void
  cancel: () => void
} {
  bindings.add(binding)
  startListening()
  refresh()
  return {
    unregister() {
      if (!bindings.delete(binding)) return
      if (held === binding) cancelHeld()
      if (!bindings.size) stopListening()
    },
    refresh,
    cancel() {
      if (held === binding) cancelHeld()
    },
  }
}
