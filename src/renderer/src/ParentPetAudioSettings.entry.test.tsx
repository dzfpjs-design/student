import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { ParentPetAudioTeaching } from './ParentPetAudioTeaching'
it('has a real configuration entry when the original audio provider is not configured', async () => {
  const request = async <T,>(path: string): Promise<T> => (path.endsWith('authorization')
    ? { child_id: 'child', configured: false, current: false, grant: null }
    : { child_id: 'child', items: [] }) as T
  render(<ParentPetAudioTeaching childId="child" request={request} />)
  expect(await screen.findByRole('button', { name: '配置原音模型' })).toBeEnabled()
})
