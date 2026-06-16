export type StepStatus = 'running' | 'success' | 'error'

export interface AgentStep {
  step: string
  status: StepStatus
}

export interface ResultRow {
  [key: string]: string | number | boolean | null
}

export interface AgentReplyMessage {
  id: string
  role: 'assistant'
  steps: AgentStep[]
  result: ResultRow[]
  guideQueries: string[]
  status: 'streaming' | 'success' | 'error'
  errorMessage?: string
}

export interface UserMessage {
  id: string
  role: 'user'
  content: string
}

export type ChatMessage = UserMessage | AgentReplyMessage
