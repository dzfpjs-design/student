#!/usr/bin/env node
// Stable, truthfully named entrypoint for the visible Electron/CDP acceptance.
// The implementation starts the real Electron renderer; the similarly named
// Python API preparation script is deliberately not imported here.
await import('./run-observation-quality-electron-acceptance.mjs')
