import {afterEach,expect,it,vi} from 'vitest'
import {localApi,resetRuntimeCache} from './api'
const operation={request_id:'original-operation',expected_generation:'original-generation',cleanup_manifest_sha256:'a'.repeat(64)}
afterEach(()=>{vi.unstubAllGlobals();delete window.desktop;resetRuntimeCache()})
it('routes destructive commands through main without an additional renderer POST',async()=>{
  const executeNativePrivacy=vi.fn().mockResolvedValue({status:200,body:{status:'completed'}})
  window.desktop={executeNativePrivacy} as unknown as NonNullable<typeof window.desktop>
  const fetch=vi.fn();vi.stubGlobal('fetch',fetch)
  await localApi.deleteChildData('child-a','all_history','确认删除',operation)
  await localApi.restoreBackup('backup-a','确认恢复',operation)
  await localApi.restorePortableBackup('/synthetic/backup','synthetic-passphrase','确认恢复',operation)
  expect(executeNativePrivacy).toHaveBeenCalledTimes(3)
  expect(executeNativePrivacy.mock.calls[0][0]).toEqual({path:'/v1/parent/children/child-a/delete',body:{scope:'all_history',confirmation:'确认删除',client_action_id:operation.request_id,operation}})
  expect(fetch).not.toHaveBeenCalled()
})
it('preserves controlled errors and explicitly distinguishes read-only recovery from fencing',async()=>{
  const executeNativePrivacy=vi.fn().mockResolvedValue({status:409,body:{detail:{code:'NATIVE_PRIVACY_PENDING',message:'原操作待核对。'}}})
  const recoverNativePrivacy=vi.fn().mockResolvedValue({status:200,body:{request_id:operation.request_id,status:'failed'}})
  window.desktop={executeNativePrivacy,recoverNativePrivacy} as unknown as NonNullable<typeof window.desktop>
  await expect(localApi.deleteChildData('a','media','确认删除',operation)).rejects.toMatchObject({status:409,code:'NATIVE_PRIVACY_PENDING'})
  await localApi.privacyOperationReceipt(operation.request_id)
  expect(recoverNativePrivacy).toHaveBeenLastCalledWith(operation.request_id,'',false)
  await localApi.recoverNativePrivacyOperation(operation.request_id)
  expect(recoverNativePrivacy).toHaveBeenLastCalledWith(operation.request_id,'',true)
})
