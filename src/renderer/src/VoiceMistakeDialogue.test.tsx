import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { VoiceMistakeDialogue } from './VoiceMistakeDialogue'
import type { MistakeDialogue } from './types'
import { clearVoiceCaptureContext, setVoiceQuestionCapture, voiceCaptureReference } from './voiceCaptureContext'
import { CAPTURE_SAVED_EVENT } from './deviceCommandReceipt'

const saved: MistakeDialogue = { session_id: 'session-one', child_id: 'child-one', candidate_id: 'candidate-one',
  mistake_id: null, phase: 'confirm', response_text: '我记下的答案是B，对吗？',
  state: { artifact_id: 'artifact-one', question_text: '选出读音不同的一项', submitted_answer: 'B' } }
beforeEach(() => { vi.spyOn(localApi, 'mistakeDialogue').mockResolvedValue({ dialogue: saved }) })
afterEach(() => { cleanup(); clearVoiceCaptureContext(); vi.restoreAllMocks() })

it('restores a saved non-paper dialogue without a new capture or write', async () => {
  const binding = { session_id: saved.session_id, child_id: saved.child_id, mistake_id: 'diagnostic-mistake',
    phase: 'diagnosis', item_id: 'source-dialogue', source_version: 'diagnostic-source-v1' }
  const active = { ...saved, candidate_id: null, mistake_id: binding.mistake_id, phase: binding.phase,
    state: { question_text: '计算 (24+6)×3', submitted_answer: '54' }, answer_binding: null, dialogue_binding: binding }
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: active })
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue')
  const intervention = vi.fn()
  window.addEventListener('hsc-agent-intervention', intervention)
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" mistakeId="diagnostic-mistake" />)
  expect(await screen.findByText('当时的答案：54')).toBeInTheDocument()
  window.removeEventListener('hsc-agent-intervention', intervention)
  expect(intervention).not.toHaveBeenCalled()
  expect(begin).not.toHaveBeenCalled()
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toEqual({ kind: 'mistake', binding })
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: {
    ...active, state: { ...active.state, artifact_id: 'different-paper' }, response_text: '不要替换成另一页',
  } })))
  expect(screen.queryByText('不要替换成另一页')).not.toBeInTheDocument()
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toEqual({ kind: 'mistake', binding })
})

it.each(['diagnosis', 'remediation'])('publishes a read-only %s reference without inventing answer ownership', async phase => {
  const binding = { session_id: saved.session_id, child_id: saved.child_id, mistake_id: 'mistake-one',
    phase, item_id: 'source-dialogue', source_version: 'readonly-v1' }
  const active = { ...saved, mistake_id: binding.mistake_id, phase, answer_binding: null, dialogue_binding: binding }
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(active)
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: active })
  const { unmount } = render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  await screen.findByRole('status')
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toEqual({ kind: 'mistake', binding })
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: {
    ...active, dialogue_binding: { ...binding, child_id: 'child-two' }, response_text: '另一孩子的来源',
  } })))
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toEqual({ kind: 'mistake', binding })
  expect(screen.queryByText('另一孩子的来源')).not.toBeInTheDocument()
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: {
    ...active, phase: 'paused', dialogue_binding: null,
  } })))
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toBeNull()
  unmount()
})

it('freezes the displayed mistake source and clears it when the dialogue is paused', async () => {
  const binding = { session_id: saved.session_id, child_id: saved.child_id, mistake_id: 'mistake-one',
    phase: 'attempt', item_id: 'variant-one', source_version: 'version-one' }
  const active = { ...saved, mistake_id: binding.mistake_id, phase: 'attempt', answer_binding: binding }
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(active)
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: active })
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  await screen.findByText('当时的答案：B')
  const captured = voiceCaptureReference('session-one', 'child-one')
  expect(captured.question_reference).toEqual({ kind: 'mistake', binding })
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: {
    ...saved, phase: 'paused', answer_binding: null,
  } })))
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toBeNull()
  expect(captured.question_reference).toEqual({ kind: 'mistake', binding })
})

it('restores saved voice answer without a button and ignores another child session', async () => {
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(saved)
  const speech = vi.fn()
  window.addEventListener('hsc-agent-intervention', speech)
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  expect(await screen.findByText('当时的答案：B')).toBeInTheDocument()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  expect(speech).toHaveBeenCalledTimes(1)
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: {
    ...saved, session_id: 'session-two', response_text: '其他孩子的内容'
  } })))
  expect(screen.queryByText('其他孩子的内容')).not.toBeInTheDocument()
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: {
    ...saved, phase: 'diagnosis', response_text: '你从哪一步开始不确定？'
  } })))
  expect(screen.getByRole('status')).toHaveTextContent('你从哪一步开始不确定？')
  expect(speech).toHaveBeenCalledTimes(1) // Runtime, not a second UI effect, owns reply audio.
  window.removeEventListener('hsc-agent-intervention', speech)
})

it('voice retry uses the original idempotency key after a failed begin', async () => {
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockRejectedValueOnce(new Error('network')).mockResolvedValue(saved)
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  expect(await screen.findByRole('alert')).toBeInTheDocument()
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-capture-voice-action', { cancelable: true, detail: { action: 'open', sessionId: 'session-one', childId: 'child-one' } })))
  await waitFor(() => expect(begin).toHaveBeenCalledTimes(2))
  expect(begin.mock.calls[0]).toEqual(begin.mock.calls[1])
  expect(await screen.findByText('当时的答案：B')).toBeInTheDocument()
})

it('resumes only its saved recapture receipt without clicks or duplicate readback', async () => {
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockRejectedValueOnce(new Error('receipt pending')).mockResolvedValue(saved)
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  await screen.findByRole('alert')
  const receipt = { sessionId: 'session-one', commandId: 'recapture-one', artifactId: 'artifact-one', status: 'recognized' }
  act(() => window.dispatchEvent(new CustomEvent(CAPTURE_SAVED_EVENT, { detail: { ...receipt, artifactId: 'other-page' } })))
  expect(begin).toHaveBeenCalledTimes(1)
  act(() => window.dispatchEvent(new CustomEvent(CAPTURE_SAVED_EVENT, { detail: receipt })))
  await screen.findByRole('status')
  expect(begin).toHaveBeenCalledTimes(2)
  expect(begin.mock.calls[0]).toEqual(begin.mock.calls[1])
  act(() => window.dispatchEvent(new CustomEvent(CAPTURE_SAVED_EVENT, { detail: receipt })))
  expect(begin).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})

it('reopening uses the latest saved phase, not the original begin replay', async () => {
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(saved)
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  expect(await screen.findByText('当时的答案：B')).toBeInTheDocument()
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: {
    ...saved, phase: 'scheduled', response_text: '这道题两天后再独立检查。'
  } })
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-capture-voice-action', { cancelable: true, detail: { action: 'open', sessionId: 'session-one', childId: 'child-one' } })))
  expect(await screen.findByRole('status')).toHaveTextContent('这道题两天后再独立检查。')
})

it('a new photo gets a different action key and does not replay the previous page', async () => {
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(saved)
  const { rerender } = render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  expect(await screen.findByText('当时的答案：B')).toBeInTheDocument()
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: { ...saved, state: { ...saved.state, artifact_id: 'artifact-two' } } })
  rerender(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-two" />)
  await waitFor(() => expect(begin).toHaveBeenCalledTimes(2))
  expect(begin.mock.calls[1][1]).toBe('artifact-two')
  expect(begin.mock.calls[1][2]).not.toBe(begin.mock.calls[0][2])
})

it('an in-flight opening cannot replace a newer agent reply', async () => {
  let complete: (value: MistakeDialogue) => void = () => undefined
  vi.spyOn(localApi, 'beginMistakeDialogue').mockReturnValue(new Promise((resolve) => { complete = resolve }))
  const speech = vi.fn()
  window.addEventListener('hsc-agent-intervention', speech)
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: {
    ...saved, phase: 'diagnosis', response_text: '你从哪一步开始不确定？'
  } })))
  await act(async () => { complete(saved) })
  expect(screen.getByRole('status')).toHaveTextContent('你从哪一步开始不确定？')
  expect(speech).not.toHaveBeenCalled()
  window.removeEventListener('hsc-agent-intervention', speech)
})

it.each(['child', 'page', 'binding'])('rejects a latest dialogue with a different %s before speaking or binding', async (wrong) => {
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(saved)
  const value: MistakeDialogue = { ...saved, response_text: '不属于这张原页的内容',
    child_id: wrong === 'child' ? 'child-two' : saved.child_id,
    state: { ...saved.state, artifact_id: wrong === 'page' ? 'artifact-two' : 'artifact-one' },
    ...(wrong === 'binding' ? { answer_binding: { session_id: saved.session_id, child_id: 'child-two',
      mistake_id: 'mistake-one', phase: 'attempt', item_id: 'item-one', source_version: 'v1' } } : {}) }
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: value })
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  await screen.findByRole('alert')
  expect(screen.queryByText(value.response_text)).not.toBeInTheDocument()
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toBeNull()
  expect(voiceCaptureReference('session-one', 'child-two').question_reference).toBeNull()
})

it('ignores cross-page and cross-child updates in the same session', async () => {
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(saved)
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  await screen.findByRole('status')
  for (const changed of [{ child_id: 'child-two' }, { state: { ...saved.state, artifact_id: 'artifact-two' } }]) {
    act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: {
      ...saved, ...changed, response_text: '另一来源的提问',
    } })))
    expect(screen.getByRole('status')).toHaveTextContent(saved.response_text)
  }
})

it('does not read or overwrite a newer teaching source after a late begin', async () => {
  let finish!: (value: MistakeDialogue) => void
  vi.spyOn(localApi, 'beginMistakeDialogue').mockReturnValue(new Promise(resolve => { finish = resolve }))
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  const exam = { kind: 'exam' as const, binding: { run_id: 'run-one', position: 0, item_id: 'item-one', source_version: 'v1' } }
  setVoiceQuestionCapture('session-one', 'child-one', exam)
  await act(async () => { finish(saved) })
  expect(localApi.mistakeDialogue).not.toHaveBeenCalled()
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toEqual(exam)
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
})

it.each([false, true])('releases only its own answer binding on exit (newer source=%s)', async (newer) => {
  const binding = { session_id: saved.session_id, child_id: saved.child_id, mistake_id: 'mistake-one',
    phase: 'attempt', item_id: 'variant-one', source_version: 'v1' }
  const active = { ...saved, mistake_id: binding.mistake_id, phase: 'attempt', answer_binding: binding }
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(active)
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: active })
  const { unmount } = render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  await screen.findByRole('status')
  const captured = voiceCaptureReference('session-one', 'child-one')
  const diagnostic = { kind: 'diagnostic' as const, binding: { prompt_id: 'next', item_version: 'v2' } }
  if (newer) setVoiceQuestionCapture('session-one', 'child-one', diagnostic)
  unmount()
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toEqual(newer ? diagnostic : null)
  expect(captured.question_reference).toEqual({ kind: 'mistake', binding })
})

it('coalesces matching voice retries and acknowledges failure truthfully', async () => {
  let fail!: (cause: Error) => void
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockReturnValue(new Promise((_resolve, reject) => { fail = reject }))
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  const replies = [vi.fn(), vi.fn()]
  for (const resolve of replies) act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-capture-voice-action', {
    cancelable: true, detail: { action: 'open', sessionId: 'session-one', childId: 'child-one', resolve },
  })))
  expect(begin).toHaveBeenCalledTimes(1)
  await act(async () => { fail(new Error('offline')) })
  for (const resolve of replies) expect(resolve).toHaveBeenCalledExactlyOnceWith(false)
})

it('does not claim an unscoped or other-child retry', async () => {
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockRejectedValue(new Error('offline'))
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  await screen.findByRole('alert')
  for (const scope of [{}, { sessionId: 'session-one', childId: 'child-two' }]) {
    const resolve = vi.fn()
    const event = new CustomEvent('hsc-mistake-capture-voice-action', { cancelable: true, detail: { action: 'open', ...scope, resolve } })
    act(() => window.dispatchEvent(event))
    expect(event.defaultPrevented).toBe(false)
    expect(resolve).not.toHaveBeenCalled()
  }
  expect(begin).toHaveBeenCalledTimes(1)
})

it('does not treat a missing current record as permission to replay the initial result', async () => {
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(saved)
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: null })
  const opened = vi.fn()
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" onOpened={opened} />)
  await screen.findByRole('alert')
  expect(opened).toHaveBeenCalledExactlyOnceWith(false)
  expect(screen.queryByText('当时的答案：B')).not.toBeInTheDocument()
})

it('waits for the current page read before reporting an opening outcome', async () => {
  let finish!: (value: { dialogue: MistakeDialogue | null }) => void
  vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(saved)
  vi.mocked(localApi.mistakeDialogue).mockReturnValue(new Promise(resolve => { finish = resolve }))
  const opened = vi.fn()
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" onOpened={opened} />)
  await waitFor(() => expect(localApi.mistakeDialogue).toHaveBeenCalledTimes(1))
  expect(opened).not.toHaveBeenCalled()
  await act(async () => { finish({ dialogue: saved }) })
  expect(opened).toHaveBeenCalledExactlyOnceWith(true, saved)
})

it('does not bind or speak a cancelled opening and can then resume with the original key', async () => {
  let finish!: (value: { dialogue: MistakeDialogue | null }) => void
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue(saved)
  vi.mocked(localApi.mistakeDialogue).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
  const abort = new AbortController()
  const opened = vi.fn()
  render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one"
    onOpened={opened} openSignal={abort.signal} />)
  await waitFor(() => expect(localApi.mistakeDialogue).toHaveBeenCalledTimes(1))
  abort.abort()
  await act(async () => { finish({ dialogue: saved }) })
  expect(opened).toHaveBeenCalledExactlyOnceWith(false)
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  expect(voiceCaptureReference('session-one', 'child-one').question_reference).toBeNull()
  const resolve = vi.fn()
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-capture-voice-action', { cancelable: true,
    detail: { action: 'open', sessionId: 'session-one', childId: 'child-one', resolve } })))
  await screen.findByRole('status')
  expect(resolve).toHaveBeenCalledExactlyOnceWith(true)
  expect(begin.mock.calls[0]).toEqual(begin.mock.calls[1])
})

it('retires an old page immediately and never reads its late opening into the replacement', async () => {
  let finish!: (value: MistakeDialogue) => void
  const next = { ...saved, state: { ...saved.state, artifact_id: 'artifact-two', question_text: '新题' } }
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockReturnValueOnce(new Promise(resolve => { finish = resolve })).mockResolvedValue(next)
  vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: next })
  const { rerender } = render(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-one" />)
  rerender(<VoiceMistakeDialogue sessionId="session-one" childId="child-one" artifactId="artifact-two" />)
  await screen.findByText('新题')
  await act(async () => { finish(saved) })
  expect(screen.queryByText(saved.state.question_text!)).not.toBeInTheDocument()
  expect(localApi.mistakeDialogue).toHaveBeenCalledTimes(1)
  expect(begin).toHaveBeenCalledTimes(2)
})
