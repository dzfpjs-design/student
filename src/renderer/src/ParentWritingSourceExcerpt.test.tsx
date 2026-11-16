import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ParentWritingSourceExcerpt } from './ParentWritingSourceExcerpt'
afterEach(cleanup)

describe('read-only original writing evidence',()=>{
  it('shows exact task and separate task points rather than internal source JSON',()=>{
    const text=JSON.stringify({schema:'pet_writing_source_v1',prompt:'Invite Sam to a concert.',task_points:['Say when.','Say where.'],min_words:35,max_words:55,knowledge_node_id:'internal-node'})
    const {container}=render(<ParentWritingSourceExcerpt text={text}/>);
    expect(screen.getByText('Invite Sam to a concert.')).toBeTruthy()
    expect(screen.getByRole('list').textContent).toContain('Say when.')
    expect(screen.getByText('建议字数：35–55')).toBeTruthy()
    expect(container.querySelector('details')?.open).toBe(false)
    expect(container.querySelector('button')).toBeNull()
  })
  it('does not invent typed interpretation for a malformed task list',()=>{
    const text='{"schema":"pet_writing_source_v1","prompt":"Original","task_points":[7]}'
    render(<ParentWritingSourceExcerpt text={text}/>)
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.getByText(text)).toBeTruthy()
  })
  it('preserves ordinary non-typed original material',()=>{
    render(<ParentWritingSourceExcerpt text="家长提供的完整题干。"/>)
    expect(screen.getByText('家长提供的完整题干。')).toBeTruthy()
  })
})
