
import {describe,expect,it} from 'vitest'
import {L523ContentFreshnessMonitor,isL523ObservationSource} from './frameHealth'
const frame=()=>({data:new Uint8ClampedArray([30,60,90,255,150,180,210,255]),width:2,height:1})
describe('L523 repeated exact pixels are bounded content uncertainty',()=>{
 it('requests one recheck after 15 continuously observed intervals and preserves the bound across source replacement',()=>{
  const m=new L523ContentFreshnessMonitor(),a={},b={},f=frame()
  for(let i=0;i<15;i++)expect(m.inspect(f,a,i*2000)).toEqual({unconfirmed:false,recheckSource:false})
  expect(m.inspect(f,a,30_000)).toEqual({unconfirmed:true,recheckSource:true})
  for(let i=16;i<20;i++)expect(m.inspect(f,a,i*2000)).toEqual({unconfirmed:true,recheckSource:false})
  const replacementFirst=frame();replacementFirst.data[0]++
  expect(m.inspect(replacementFirst,b,40_000)).toEqual({unconfirmed:true,recheckSource:false})
  for(let i=21;i<150;i++)expect(m.inspect(replacementFirst,b,i*2000)).toEqual({unconfirmed:true,recheckSource:false})
  const changed={...replacementFirst,data:replacementFirst.data.slice()};changed.data[6]++
  expect(m.inspect(changed,b,300_000)).toEqual({unconfirmed:false,recheckSource:false})
  for(let i=1;i<15;i++)expect(m.inspect(changed,b,300_000+i*2000).unconfirmed).toBe(false)
  expect(m.inspect(changed,b,330_000)).toEqual({unconfirmed:true,recheckSource:false})
  m.clearContent();const c={};m.inspect(changed,c,340_000)
  for(let i=1;i<=15;i++)expect(m.inspect(changed,c,340_000+i*2000).recheckSource).toBe(false)
 })
 it('compares every color pixel and copies the original ephemeral buffer',()=>{
  const m=new L523ContentFreshnessMonitor(),a={},f=frame()
  m.inspect(f,a,0);f.data[6]++
  expect(m.inspect(f,a,2000)).toEqual({unconfirmed:false,recheckSource:false})
  for(let i=2;i<=15;i++)expect(m.inspect(f,a,i*2000).unconfirmed).toBe(false)
  expect(m.inspect(f,a,32_000).recheckSource).toBe(true)
 })
 it('does not count an unsampled gap, source change, or backwards monotonic tick',()=>{
  const m=new L523ContentFreshnessMonitor(),a={},b={},f=frame()
  m.inspect(f,a,0);expect(m.inspect(f,a,40_000).recheckSource).toBe(false)
  for(let i=1;i<15;i++)expect(m.inspect(f,a,40_000+i*2000).unconfirmed).toBe(false)
  expect(m.inspect(f,b,70_000).unconfirmed).toBe(false)
  expect(m.inspect(f,b,1).unconfirmed).toBe(false)
 })
 it('normal explicit reset releases old content and cannot make a previous child source block a new one',()=>{
  const m=new L523ContentFreshnessMonitor(),a={},f=frame()
  for(let i=0;i<=15;i++)m.inspect(f,a,i*2000)
  expect(m.unconfirmed).toBe(true);m.reset();expect(m.unconfirmed).toBe(false)
  const b={};expect(m.inspect(f,b,50_000)).toEqual({unconfirmed:false,recheckSource:false})
  for(let i=1;i<15;i++)m.inspect(f,b,50_000+i*2000)
  expect(m.inspect(f,b,80_000)).toEqual({unconfirmed:true,recheckSource:true})
 })
 it('limits the quirk to the known L523 observation label',()=>{
  for(const s of ['L523','L523 (1d6b:0102)',' l523 Camera '])expect(isL523ObservationSource(s)).toBe(true)
  for(const s of ['Usb HDCamera (eba4:1303)','Camera','L5234',''])expect(isL523ObservationSource(s)).toBe(false)
 })
})
