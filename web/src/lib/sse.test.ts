import { describe, expect, it } from 'vitest'

import { parseSseChunk } from './sse'

describe('parseSseChunk', () => {
  it('parses complete SSE events and preserves partial trailing content', () => {
    const raw = [
      'data: {"step":"解析提问意图","status":"running","data":null,"finished":false}',
      '',
      'data: {"step":"解析提问意图","status":"success","data":null,"finished":false}',
      '',
      'data: {"step":"生成 SQL","status":"success","data":[{"product_name":"Magic6 Pro","sales_amount":5999}],"finished":true}',
      '',
      'data: {"step":"执行',
    ].join('\n')

    const result = parseSseChunk(raw)

    expect(result.events).toHaveLength(3)
    expect(result.events[0]).toMatchObject({
      step: '解析提问意图',
      status: 'running',
      finish: false,
    })
    expect(result.events[2]).toMatchObject({
      step: '生成 SQL',
      status: 'success',
      finish: true,
      data: [{ product_name: 'Magic6 Pro', sales_amount: 5999 }],
    })
    expect(result.rest).toBe('data: {"step":"执行')
  })

  it('ignores malformed payloads without crashing', () => {
    const raw = [
      'data: not-json',
      '',
      'data: {"step":"执行查询","status":"running","data":null,"finished":false}',
      '',
    ].join('\n')

    const result = parseSseChunk(raw)

    expect(result.events).toHaveLength(1)
    expect(result.events[0]?.step).toBe('执行查询')
    expect(result.rest).toBe('')
  })

  it('parses guide queries on finished events', () => {
    const raw = [
      'data: {"step":"解析提问意图","status":"success","data":null,"finished":true,"guide_questions":["帮我统计一下上个季度销售额","xx","yy","zz"]}',
      '',
    ].join('\n')

    const result = parseSseChunk(raw)

    expect(result.events).toHaveLength(1)
    expect(result.events[0]).toMatchObject({
      step: '解析提问意图',
      status: 'success',
      data: null,
      finish: true,
      guide_queries: ['帮我统计一下上个季度销售额', 'xx', 'yy', 'zz'],
    })
    expect(result.rest).toBe('')
  })
})
