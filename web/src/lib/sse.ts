export type AgentStepStatus = 'running' | 'success' | 'error'
export type AgentResultValue = string | number | boolean | null

export interface AgentEvent {
  step: string
  status: AgentStepStatus
  data: Record<string, AgentResultValue>[] | null
  finished: boolean
  guide_questions?: string[] | null
}

export interface SseChunkParseResult {
  events: AgentEvent[]
  rest: string
}

export function parseSseChunk(input: string): SseChunkParseResult {
  const segments = input.split('\n\n')
  const hasTrailingSeparator = input.endsWith('\n\n')
  const completedSegments = hasTrailingSeparator ? segments.filter(Boolean) : segments.slice(0, -1).filter(Boolean)
  const rest = hasTrailingSeparator ? '' : segments.at(-1) ?? ''

  const events = completedSegments.flatMap((segment) => {
    const payload = segment
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim())
      .join('')

    if (!payload) {
      return []
    }

    try {
      return [JSON.parse(payload) as AgentEvent]
    } catch {
      return []
    }
  })

  return { events, rest }
}
