import { act, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AgentReplyText, createAgentReplyStore } from './AgentReplyText'

describe('live agent reply', () => {
  it('updates streaming text without re-rendering the workspace and retains the final reply', () => {
    const store = createAgentReplyStore('我在这儿。')
    let renders = 0
    function Workspace() { renders++; return <div><AgentReplyText store={store} /></div> }
    const view = render(<Workspace />)
    act(() => { store.set('第一'); store.set('第一句话'); store.set('第一句话。') })
    expect(screen.getByText('第一句话。')).toBeTruthy()
    expect(renders).toBe(1)
    act(() => store.set('设备暂时不可用'))
    expect(screen.getByText('设备暂时不可用')).toBeTruthy()
    view.unmount()
    act(() => store.set('已卸载'))
  })
})
