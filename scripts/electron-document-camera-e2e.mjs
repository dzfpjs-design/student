#!/usr/bin/env node
// Visible Electron/CDP check for the real document-camera path. It exercises
// the exact bound-device failure/retry surface when the high camera is absent;
// it is not an OCR accuracy benchmark and it never captures a page.
if (!process.argv.includes('--document-camera-unavailable')) process.argv.push('--document-camera-unavailable')
await import('./run-observation-quality-electron-acceptance.mjs')
