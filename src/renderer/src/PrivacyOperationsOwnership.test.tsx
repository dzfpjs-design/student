import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { PrivacyOperations } from './ParentCenter'
import type { CaptureArtifactSummary, PrivacyPreferences, SystemHealth } from './types'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('clears destructive confirmation and rejects late capture lists when changing children', async () => {
  let finish!: (items: CaptureArtifactSummary[]) => void
  vi.spyOn(localApi, 'listBackups').mockResolvedValue([])
  vi.spyOn(localApi, 'captureArtifacts').mockImplementation(async (child) => child === 'a'
    ? new Promise(resolve => { finish = resolve }) : [])
  const health = { database_integrity: 'ok', maintenance: {}, last_backup: null } as SystemHealth
  const preferences = { cloud_audio_allowed: false, raw_media_storage_allowed: false } as PrivacyPreferences
  const props = { initial: preferences, health, onHealthChange: vi.fn() }
  const view = render(<PrivacyOperations {...props} childId="a" />)
  fireEvent.change(screen.getByLabelText('删除确认'), { target: { value: '确认删除' } })
  view.rerender(<PrivacyOperations {...props} childId="b" />)
  await act(async () => { finish([{ artifact_id: 'other-child-page', artifact_type: 'ocr',
    created_at: '2026-09-07T12:00:00Z', extracted_text_chars: 917, raw_file_stored: false } as CaptureArtifactSummary]) })
  expect(screen.getByLabelText('删除确认')).toHaveValue('')
  expect(screen.getByRole('button', { name: /^删除$/ })).toBeDisabled()
  expect(screen.queryByText(/917字/)).not.toBeInTheDocument()
})
