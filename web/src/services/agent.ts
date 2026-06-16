import axios from 'axios'

import type { AgentReplyMessage, ResultRow } from '@/types/agent'
import { parseSseChunk, type AgentEvent, type AgentResultValue } from '@/lib/sse'

const agentApi = axios.create({
  baseURL: 'http://127.0.0.1:8000',
  headers: {
    Accept: 'text/event-stream',
    'Content-Type': 'application/json',
  },
  responseType: 'text',
})

interface QueryOptions {
  signal?: AbortSignal
  onStep: (message: AgentReplyMessage) => void
}

function toErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    return error.message || '查询请求失败，请稍后重试。'
  }

  if (error instanceof Error) {
    return error.message
  }

  return '查询请求失败，请稍后重试。'
}

function mergeReplyMessage(current: AgentReplyMessage, event: AgentEvent): AgentReplyMessage {
  const stepIndex = current.steps.findIndex((item) => item.step === event.step)
  const nextSteps = [...current.steps]

  if (stepIndex >= 0) {
    const existingStep = nextSteps[stepIndex]
    if (existingStep) {
      nextSteps[stepIndex] = { ...existingStep, status: event.status }
    }
  } else {
    nextSteps.push({ step: event.step, status: event.status })
  }
  return {
    ...current,
    steps: nextSteps,
    result: event.finished && Array.isArray(event.data) ? event.data : current.result,
    guideQueries:
      event.finished && event.guide_questions && event.guide_questions.length > 0
        ? event.guide_questions
        : current.guideQueries,
    status: event.finished ? 'success' : current.status,
  }
}

function isResultValue(value: unknown): value is AgentResultValue {
  return value === null || ['string', 'number', 'boolean'].includes(typeof value)
}

function normalizeEvent(event: AgentEvent): AgentEvent | null {
  if (!event.step || !event.status) {
    return null
  }

  const guideQueries = Array.isArray(event.guide_questions)
    ? event.guide_questions.filter(
        (item): item is string => typeof item === 'string' && item.trim().length > 0,
      )
    : null

  if (!Array.isArray(event.data)) {
    return {
      ...event,
      data: null,
      guide_queries: guideQueries,
    }
  }

  const normalizedRows = event.data.map((row) => {
    const normalizedRow: ResultRow = {}

    for (const [key, value] of Object.entries(row)) {
      normalizedRow[key] = isResultValue(value) ? value : String(value)
    }

    return normalizedRow
  })

  return {
    ...event,
    data: normalizedRows,
    guide_queries: guideQueries,
  }
}

export async function streamAgentQuery(query: string, options: QueryOptions): Promise<void> {
  let processedLength = 0
  let rest = ''
  let message: AgentReplyMessage = {
    id: crypto.randomUUID(),
    role: 'assistant',
    steps: [],
    result: [],
    guideQueries: [],
    status: 'streaming',
  }

  await agentApi.post(
    '/chat',
    { content: query },
    {
      signal: options.signal,
      onDownloadProgress: (progressEvent) => {
        const target = progressEvent.event?.target as XMLHttpRequest | undefined
        const responseText = target?.responseText

        if (typeof responseText !== 'string' || responseText.length <= processedLength) {
          return
        }

        const chunk = responseText.slice(processedLength)
        processedLength = responseText.length
        const parsed = parseSseChunk(rest + chunk)
        rest = parsed.rest

        for (const event of parsed.events) {
          const normalizedEvent = normalizeEvent(event)

          if (!normalizedEvent) {
            continue
          }

          message = mergeReplyMessage(message, normalizedEvent)
          options.onStep(message)
        }
      },
    },
  )

  if (rest.trim()) {
    const parsed = parseSseChunk(`${rest}\n\n`)

    for (const event of parsed.events) {
      const normalizedEvent = normalizeEvent(event)

      if (!normalizedEvent) {
        continue
      }

      message = mergeReplyMessage(message, normalizedEvent)
      options.onStep(message)
    }
  }
}

export { toErrorMessage }
