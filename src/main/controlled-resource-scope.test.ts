// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { ControlledResourceScope } from './controlled-resource-scope'

describe('native resource identity is not an empty pointer', () => {
  it('rejects missing identity and restored sessions with no local exit proof', () => {
    const scope = new ControlledResourceScope()
    expect(scope.check(undefined, [])).toMatchObject({ closed: false, proof: 'unknown' })
    expect(scope.check('restored-game', [])).toMatchObject({ closed: false, proof: 'unknown' })
  })
  it('only permits the exact original live resource to be signalled', () => {
    const scope = new ControlledResourceScope()
    expect(scope.check('original', ['new'])).toMatchObject({ closed: false, proof: 'identity_mismatch' })
    expect(scope.check('original', ['original', 'new'])).toMatchObject({ closed: false })
    expect(scope.check('original', ['original'])).toBeNull()
  })
  it('retries actual exit in the same process, not a different identity or main restart', () => {
    const scope = new ControlledResourceScope()
    scope.recordClosed('original')
    expect(scope.check('original', [])).toEqual({ closed: true, sessionId: 'original', proof: 'observed_closed' })
    expect(scope.check('other', [])).toMatchObject({ closed: false })
    expect(new ControlledResourceScope().check('original', [])).toMatchObject({ closed: false, proof: 'unknown' })
    expect(scope.check('original', ['new'])).toMatchObject({ closed: false })
  })
})
