import { describe, expect, it } from 'vitest'

import { formatResultValue, hasDisplayableResult } from './result-display'

describe('result display helpers', () => {
  it('treats empty results as no result', () => {
    expect(hasDisplayableResult([])).toBe(false)
    expect(hasDisplayableResult([{ gmv: null }])).toBe(false)
    expect(hasDisplayableResult([{ gmv: 'None' }])).toBe(false)
    expect(hasDisplayableResult([{ gmv: '   ' }])).toBe(false)
  })

  it('keeps meaningful result rows visible', () => {
    expect(hasDisplayableResult([{ gmv: 0 }])).toBe(true)
    expect(hasDisplayableResult([{ gmv: null, order_count: 3 }])).toBe(true)
  })

  it('formats placeholder values for table cells', () => {
    expect(formatResultValue(null)).toBe('-')
    expect(formatResultValue('None')).toBe('-')
    expect(formatResultValue(' null ')).toBe('-')
    expect(formatResultValue(0)).toBe(0)
  })
})
