import {afterEach,expect,it,vi}from 'vitest'
import {pauseHiddenLearningReceivers,nativeCapabilityNeedsLearningSurface}from './learningSurfaceForeground'
afterEach(()=>vi.restoreAllMocks())
it('a hidden saved learning surface cannot claim voice actions, while camera/privacy controls keep their own original scope',()=>{
 const learning=vi.fn(),camera=vi.fn(),privacy=vi.fn(),resolve=vi.fn()
 window.addEventListener('hsc-study-learning-voice-action',learning);window.addEventListener('hsc-camera-device-command',camera);window.addEventListener('hsc-privacy-pause',privacy)
 const release=pauseHiddenLearningReceivers()
 try{
  window.dispatchEvent(new CustomEvent('hsc-study-learning-voice-action',{detail:{resolve},cancelable:true}));window.dispatchEvent(new Event('hsc-camera-device-command'));window.dispatchEvent(new Event('hsc-privacy-pause'))
  expect(learning).not.toHaveBeenCalled();expect(resolve).toHaveBeenCalledWith(false);expect(camera).toHaveBeenCalledTimes(1);expect(privacy).toHaveBeenCalledTimes(1)
  release();window.dispatchEvent(new CustomEvent('hsc-study-learning-voice-action',{detail:{resolve}}));expect(learning).toHaveBeenCalledTimes(1)
 }finally{release();window.removeEventListener('hsc-study-learning-voice-action',learning);window.removeEventListener('hsc-camera-device-command',camera);window.removeEventListener('hsc-privacy-pause',privacy)}
})
it('new concrete practice may show its original learning UI; an answer may never gain a different reference by switching surfaces',()=>{
 expect(nativeCapabilityNeedsLearningSurface('learning.explain',false)).toBe(true);expect(nativeCapabilityNeedsLearningSurface('task.start',false)).toBe(true)
 expect(nativeCapabilityNeedsLearningSurface('learning.evaluate_response',true)).toBe(false);expect(nativeCapabilityNeedsLearningSurface('device.status',false)).toBe(false)
})
