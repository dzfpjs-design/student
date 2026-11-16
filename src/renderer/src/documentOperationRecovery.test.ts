import { expect, it, vi } from 'vitest'
import { recoverDocumentOperations, type DocumentDeliveryReceipt } from './documentOperationRecovery'

const receipt: DocumentDeliveryReceipt = { session_id: 'session', client_action_id: 'original', operation: 'revision',
  status: 'saved', local_only: true, replay_allowed: false, artifact_id: 'page', revision_id: 'version' }

it('acknowledges saved delivery only and leaves an unconfirmed original request untouched', async () => {
  const read = vi.fn().mockResolvedValue([receipt, { ...receipt, client_action_id: 'pending', status: 'unconfirmed' }])
  const ack = vi.fn().mockResolvedValue(receipt)
  await expect(recoverDocumentOperations('session', read, ack, () => true)).resolves.toEqual({ saved: 1, unconfirmed: 1 })
  expect(ack).toHaveBeenCalledExactlyOnceWith('session', 'revision', 'original')
})

it.each([{ session_id: 'other-child-session' }, { replay_allowed: true }, { artifact_id: '' }])('rejects an invalid scoped receipt %j', async change => {
  const ack = vi.fn()
  await expect(recoverDocumentOperations('session', async () => [{ ...receipt, ...change }] as DocumentDeliveryReceipt[], ack, () => true)).rejects.toThrow()
  expect(ack).not.toHaveBeenCalled()
})

it('does not acknowledge results after the active session was superseded', async () => {
  const ack = vi.fn()
  await expect(recoverDocumentOperations('session', async () => [receipt], ack, () => false)).rejects.toMatchObject({ name: 'AbortError' })
  expect(ack).not.toHaveBeenCalled()
})

it('rejects a different source returned during acknowledgment', async () => {
  await expect(recoverDocumentOperations('session', async () => [receipt], async () => ({ ...receipt, revision_id: 'other-version' }), () => true))
    .rejects.toThrow('SOURCE_CHANGED')
})
