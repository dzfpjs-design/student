import {createHmac} from 'node:crypto'
import type {ControlledLaunchReceipt} from '../shared/controlledLaunch'
export type NativeAbsenceVersion={nonce:string;revision:number}
export function signNativeAbsence(key:string|undefined,binding:ControlledLaunchReceipt,proof:string,version:NativeAbsenceVersion):string{
  if(!key||!/^[a-f0-9]{64}$/.test(key))throw Error('NATIVE_ATTESTATION_UNAVAILABLE')
  if(!['cancelled_before_dispatch','owner_exit_before_registration'].includes(proof)||!Number.isSafeInteger(version.revision)||version.revision<2||!/^[a-f0-9-]{36}$/.test(version.nonce))throw Error('NATIVE_ATTESTATION_INVALID')
  const values=['native_launch_absence_v1',binding.childId,binding.sessionId,binding.kind,binding.requestId,
    binding.intentId,binding.activityId,proof,version.nonce,version.revision]
  return createHmac('sha256',Buffer.from(key,'hex')).update(JSON.stringify(values),'utf8').digest('hex')
}
