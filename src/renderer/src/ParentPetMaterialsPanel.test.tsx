import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { ParentPetMaterialsPanel } from './ParentPetMaterialsPanel'
import type { ParentPetCollection, ParentPetMaterial, ParentPetMedia, ParentPetPrompt } from './petMaterialSupplyTypes'
import { clearParentPetMaterialPending, parentPetPendingPrefix } from './petMaterialSupplyTypes'

const voice = vi.hoisted(() => ({ play: vi.fn(), stop: vi.fn() }))
vi.mock('./LocalInputs', () => ({ queueLocalPcm: voice.play, cancelLocalSpeech: voice.stop }))

const prompt: ParentPetPrompt = { child_id: 'child-a', origin_session_id: 'session-a', prompt_id: 'private-prompt',
  item_version: 'private-version', diagnostic_item_id: 'original-item', status: 'waiting_answer', created_at: '2026-09-07T12:00:00Z',
  item: { track: 'pet', prompt: 'Where should Leo meet Anna?', stimulus_text: 'Meet me in front of the library.', pet_audio_modality: 'listening' } }
const material: ParentPetMaterial = { artifact_id: 'material-a', status: 'completed', prompt_id: prompt.prompt_id,
  item_version: prompt.item_version, diagnostic_item_id: prompt.diagnostic_item_id, origin: 'local_synthesized_practice',
  original_recording_verified: false, parent_review_required: true, local_only: true, cloud_transmission_allowed: false }
const media: ParentPetMedia = { version: 'v1', media_id: 'media-a', child_id: prompt.child_id, session_id: prompt.origin_session_id,
  prompt_id: prompt.prompt_id, item_version: prompt.item_version, diagnostic_item_id: prompt.diagnostic_item_id, source_version: 'source-v1',
  kind: 'listening', artifact_id: material.artifact_id, status: 'paused', revision: 1, generation: 1, frame_position: 0,
  resume_required: true, source_reviewed: false, receipt_grace_ms: 2000,
  audio: { status: 'usable_signal', sha256: 'a'.repeat(64), frames: 16000, sample_rate: 16000, channels: 1, sample_width: 2 },
  qualification: { skill_qualified: false, listening_delivery_qualified: false, blockers: [], required_play_count: 2 },
  local_only: true, cloud_transmission_allowed: false }
function collection(): ParentPetCollection { return { child_id: prompt.child_id, session_id: prompt.origin_session_id,
  prompt_id: prompt.prompt_id, item_version: prompt.item_version, listening: [structuredClone(media)], speaking: [], materials: [structuredClone(material)],
  local_only: true, cloud_transmission_allowed: false } }
function requests(data = collection()) {
  return vi.fn(async (path: string, init?: RequestInit): Promise<any> => {
    if (!init?.method) return structuredClone(data)
    const body = JSON.parse(init.body as string)
    if (path.endsWith('/materialize')) return { material }
    if (path.endsWith('/begin')) return { media }
    if (path.endsWith('/source-review')) { data.listening[0].source_reviewed = body.operation === 'approve'; return { media: data.listening[0] } }
    throw new Error('Unexpected mutation')
  })
}
function mount(request = requests(), preview = vi.fn()) {
  return { ...render(<ParentPetMaterialsPanel childId={prompt.child_id} prompts={[prompt]} request={request} requestPreviewAudio={preview} />), request, preview }
}
async function choosePrompt(): Promise<void> {
  fireEvent.change(screen.getByRole('combobox', { name: '选择原题' }), { target: { value: `${prompt.origin_session_id}:${prompt.prompt_id}:${prompt.item_version}` } })
  await waitFor(() => expect(screen.getByRole('combobox', { name: '选择原音' })).toBeEnabled())
}
function chooseMedia(): void { fireEvent.change(screen.getByRole('combobox', { name: '选择原音' }), { target: { value: media.media_id } }) }
const posts = (request: ReturnType<typeof requests>) => request.mock.calls.filter(([, init]) => init?.method === 'POST')

beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks()
  voice.play.mockImplementation(async (_pcm, _rate, _sid, onStart) => { onStart?.(); return 'finished' })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Parent PET materials', () => {
  it('labels persisted school and PET scopes separately in one original-audio list', () => {
    const school = { ...prompt, prompt_id: 'school-prompt', diagnostic_item_id: 'school-question',
      item: { ...prompt.item, track: 'school' as const } }
    const request = requests()
    render(<ParentPetMaterialsPanel childId={prompt.child_id} prompts={[prompt, school]}
      request={request} requestPreviewAudio={vi.fn()} />)
    expect(screen.getByRole('heading', { name: '听力原材料' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /^校内 · Where should Leo meet Anna/ })).toHaveValue('session-a:school-prompt:private-version')
    expect(screen.getByRole('option', { name: /^PET · Where should Leo meet Anna/ })).toHaveValue('session-a:private-prompt:private-version')
    expect(request).not.toHaveBeenCalled()
  })

  it('does not auto-select, generate, bind, play or score and clearly labels synthesized practice', async () => {
    const { request, preview } = mount()
    expect(request).not.toHaveBeenCalled()
    expect(screen.getByText(/不是官方真题录音/)).toBeInTheDocument()
    await choosePrompt()
    expect(posts(request)).toHaveLength(0)
    expect(preview).not.toHaveBeenCalled()
    expect(voice.play).not.toHaveBeenCalled()
    const content = document.body.cloneNode(true) as HTMLElement
    content.querySelectorAll('style').forEach(e => e.remove())
    expect(content.textContent).not.toContain('private-')
  })

  it('explicitly creates the pinned material with no HTTP text or client role', async () => {
    const { request } = mount(); await choosePrompt()
    fireEvent.click(screen.getByRole('button', { name: '制作本机练习音频' }))
    await waitFor(() => expect(screen.getByText('材料记录已保存，可以核对并试听。')).toBeInTheDocument())
    expect(posts(request)).toHaveLength(1)
    const [path, init] = posts(request)[0]
    expect(path).toBe('/v1/sessions/session-a/pet-media/prompt/private-prompt/materialize')
    expect(JSON.parse(init!.body as string)).toEqual({ item_version: prompt.item_version, client_action_id: expect.any(String) })
  })

  it('persists ambiguous mutation and retries exact original key only after parent action following restart', async () => {
    const data = collection(), request = requests(data)
    request.mockImplementationOnce(async () => data).mockImplementationOnce(async () => { throw new Error('回执未收到') })
    const first = mount(request); await choosePrompt()
    fireEvent.click(screen.getByRole('button', { name: '制作本机练习音频' }))
    await screen.findByText('回执未收到')
    const original = posts(request)[0][1]!.body
    first.unmount()
    const restarted = mount(request); await choosePrompt()
    expect(posts(request)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '重试原操作' }))
    await waitFor(() => expect(posts(request)).toHaveLength(2))
    expect(posts(request)[1][1]!.body).toBe(original)
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试原操作' })).not.toBeInTheDocument())
    restarted.unmount()
  })

  it('binds a selected artifact rather than selecting a different media or active prompt', async () => {
    const data = collection(); data.listening = []
    const { request } = mount(requests(data)); await choosePrompt()
    fireEvent.change(screen.getByRole('combobox', { name: '选择材料' }), { target: { value: material.artifact_id } })
    fireEvent.click(screen.getByRole('button', { name: '载入供试听' }))
    await waitFor(() => expect(posts(request)).toHaveLength(1))
    expect(JSON.parse(posts(request)[0][1]!.body as string)).toMatchObject({ prompt_id: prompt.prompt_id, item_version: prompt.item_version, artifact_id: material.artifact_id })
  })

  it('requires actual full audition plus a parent confirmation and never sends child playback receipts', async () => {
    const bytes = new ArrayBuffer(44), digest = await webcrypto.subtle.digest('SHA-256', bytes)
    const data = collection(); data.listening[0].audio.sha256 = Buffer.from(digest).toString('hex')
    vi.stubGlobal('crypto', webcrypto)
    vi.stubGlobal('OfflineAudioContext', class { decodeAudioData() { return Promise.resolve({ sampleRate: 16000, length: 16000, numberOfChannels: 1, getChannelData: () => new Float32Array(16000) }) } })
    const { request, preview } = mount(requests(data), vi.fn(async () => bytes)); await choosePrompt(); chooseMedia()
    expect(screen.getByRole('button', { name: '确认来源' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '试听原音' }))
    const checkbox = screen.getByRole('checkbox')
    await waitFor(() => expect(checkbox).toBeEnabled())
    expect(screen.getByRole('button', { name: '确认来源' })).toBeDisabled()
    fireEvent.click(checkbox); fireEvent.click(screen.getByRole('button', { name: '确认来源' }))
    await waitFor(() => expect(posts(request)).toHaveLength(1))
    expect(preview).toHaveBeenCalledWith(prompt.origin_session_id, media.media_id, data.listening[0].audio.sha256, expect.any(AbortSignal))
    expect(posts(request)[0][0]).toMatch(/source-review$/)
    expect(JSON.parse(posts(request)[0][1]!.body as string)).toMatchObject({operation:'approve',full_spoken_reference:true})
    expect(request.mock.calls.some(([path]) => /playback|assessment|diagnostic.*answer/.test(path))).toBe(false)
  })

  it('rejects changed audio and does not offer source approval or TTS fallback', async () => {
    vi.stubGlobal('crypto', webcrypto)
    const { preview } = mount(requests(), vi.fn(async () => new ArrayBuffer(44))); await choosePrompt(); chooseMedia()
    fireEvent.click(screen.getByRole('button', { name: '试听原音' }))
    await screen.findByText('音频内容已变化，请重新载入。')
    expect(preview).toHaveBeenCalledTimes(1); expect(voice.play).not.toHaveBeenCalled()
    expect(screen.getByRole('checkbox')).toBeDisabled()
  })

  it('withdraws the selected current source without deleting old playback history', async () => {
    const data = collection(); data.listening[0].source_reviewed = true
    const { request } = mount(requests(data)); await choosePrompt(); chooseMedia()
    fireEvent.click(screen.getByRole('button', { name: '撤回来源确认' }))
    await waitFor(() => expect(posts(request)).toHaveLength(1))
    expect(JSON.parse(posts(request)[0][1]!.body as string).operation).toBe('retract')
    expect(request.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)
  })

  it('never displays another child response or inherits parent confirmation after switching children', async () => {
    const data = collection(); data.child_id = 'child-b'
    const { rerender, request, preview } = mount(requests(data))
    fireEvent.change(screen.getByRole('combobox', { name: '选择原题' }), { target: { value: `${prompt.origin_session_id}:${prompt.prompt_id}:${prompt.item_version}` } })
    await screen.findByText('返回材料不属于当前孩子的这道原题，未显示或确认。')
    expect(screen.getByRole('button', { name: '制作本机练习音频' })).toBeDisabled()
    rerender(<ParentPetMaterialsPanel childId="child-b" prompts={[prompt]} request={request} requestPreviewAudio={preview} />)
    expect(screen.queryByText(prompt.item.stimulus_text!)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认来源' })).not.toBeInTheDocument()
  })

  it('does not enable generation for missing original stimulus', async () => {
    const p = { ...prompt, item: { ...prompt.item, stimulus_text: null } }
    render(<ParentPetMaterialsPanel childId={prompt.child_id} prompts={[p]} request={requests()} requestPreviewAudio={vi.fn()} />)
    fireEvent.change(screen.getByRole('combobox', { name: '选择原题' }), { target: { value: `${prompt.origin_session_id}:${prompt.prompt_id}:${prompt.item_version}` } })
    await screen.findByText('缺已核准的原始听力文本，不制作猜测配音。')
    expect(screen.getByRole('button', { name: '制作本机练习音频' })).toBeDisabled()
  })

  it('clears only the requested child recovery IDs for main privacy integration', () => {
    localStorage.setItem(parentPetPendingPrefix('child-a') + 'old', '{}')
    localStorage.setItem(parentPetPendingPrefix('child-b') + 'old', '{}')
    clearParentPetMaterialPending('child-a')
    expect(localStorage.getItem(parentPetPendingPrefix('child-a') + 'old')).toBeNull()
    expect(localStorage.getItem(parentPetPendingPrefix('child-b') + 'old')).toBe('{}')
  })

  it('keeps the panel visible when browser storage throws a numeric-code DOMException', async () => {
    const storage = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('Storage unavailable', 'SecurityError') })
    try {
      mount(); await choosePrompt()
      expect(screen.getByText('本机存储不可用，无法保存重试记录；制作与来源修改已暂停。')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: '制作本机练习音频' })).toBeDisabled()
      expect(screen.getByRole('button', { name: '核对已保存状态' })).toBeEnabled()
    } finally { storage.mockRestore() }
  })
})

it('manual parent preview never receives child control authority and retired cleanup cancels only its own audio key',async()=>{
 const bytes=new ArrayBuffer(44),digest=await webcrypto.subtle.digest('SHA-256',bytes),data=collection();data.listening[0].audio.sha256=Buffer.from(digest).toString('hex')
 vi.stubGlobal('crypto',webcrypto);vi.stubGlobal('OfflineAudioContext',class {decodeAudioData(){return Promise.resolve({sampleRate:16000,length:16000,numberOfChannels:1,getChannelData:()=>new Float32Array(16000)})}})
 let release!:(value:string)=>void;voice.play.mockImplementation((_pcm,_rate,_sid,start)=>{start?.();return new Promise(resolve=>{release=resolve})})
 const view=mount(requests(data),vi.fn(async()=>bytes));await choosePrompt();chooseMedia();fireEvent.click(screen.getByRole('button',{name:'试听原音'}));await waitFor(()=>expect(voice.play).toHaveBeenCalledTimes(1))
 const options=voice.play.mock.calls[0][4];expect(options.pcmControlReference).toBeUndefined();expect(screen.getByRole('checkbox')).toBeDisabled()
 view.unmount();expect(voice.stop).toHaveBeenCalledExactlyOnceWith(`parent-pet-preview:${media.media_id}:${media.source_version}:${data.listening[0].audio.sha256}`);release('superseded')
})
