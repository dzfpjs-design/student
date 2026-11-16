import {describe,it,expect} from 'vitest'
import {resolvesKnownNameConfirmation} from './knownNameConfirmation'
describe('known pending nickname whole confirmation',()=>{
 it.each([
  ['对，就叫小知。','小知','小知'],['对，就叫小汁。','小知','小汁'],
  ['对，就叫小呀同学。','小芽同学','小呀同学'],['对，就叫Milo。','Milo','Milo'],
 ])('resolves only a complete affirmation with the same candidate pronunciation: %s',(value,pending,candidate)=>{
  expect(resolvesKnownNameConfirmation(value,pending,candidate)).toBe(true)
 })
 it.each([
  ['不，就不叫小汁','小知','小汁'],['不是，就叫小汁','小知','小汁'],['改名叫小汁','小知','小汁'],
  ['对，就叫小雨','小知','小雨'],['对，就叫小汁。暂停学习','小知','小汁'],
  ['小汁','小知','小汁'],['对，就叫小汁','小知','小雨'],['对，就叫小汁','','小汁'],
  ['对，就叫小汁',' 小知','小汁'],['对，就叫小汁','小知','小汁\n'],
 ])('does not confirm changed names, refusals, mixed commands or invalid binding: %s',(value,pending,candidate)=>{
  expect(resolvesKnownNameConfirmation(value,pending,candidate)).toBe(false)
 })
})
