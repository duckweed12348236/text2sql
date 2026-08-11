import type { ResultRow } from '@/types/agent'

type ResultValue = ResultRow[string] | undefined

function isPlaceholderString(value: string): boolean {
  const normalizedValue = value.trim().toLowerCase()

  return normalizedValue.length === 0 || normalizedValue === 'none' || normalizedValue === 'null'
}

function isDisplayableValue(value: ResultValue): boolean {
  if (value == null) {
    return false
  }

  if (typeof value === 'string') {
    return !isPlaceholderString(value)
  }

  return true
}

export function hasDisplayableResult(rows: ResultRow[]): boolean {
  if (rows.length === 0) {
    return false
  }

  return rows.some((row) => Object.values(row).some((value) => isDisplayableValue(value)))
}

export function formatResultValue(value: ResultValue): string | number | boolean {
  if (!isDisplayableValue(value)) {
    return '-'
  }

  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return value
  }

  return '-'
}
