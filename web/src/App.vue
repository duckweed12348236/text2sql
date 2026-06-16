<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'

import { formatResultValue, hasDisplayableResult } from '@/lib/result-display'
import { streamAgentQuery, toErrorMessage } from '@/services/agent'
import type { AgentReplyMessage, ChatMessage, ResultRow } from '@/types/agent'

const inputValue = ref('')
const messages = ref<ChatMessage[]>([])
const isLoading = ref(false)
const scrollContainer = ref<HTMLElement | null>(null)

let activeController: AbortController | null = null

const canSend = computed(() => inputValue.value.trim().length > 0)
const emptyResultMessage = '\u6ca1\u6709\u67e5\u8be2\u5230\u60a8\u60f3\u8981\u7684\u7ed3\u679c.'

function createReplyMessage(): AgentReplyMessage {
  return {
    id: crypto.randomUUID(),
    role: 'assistant',
    steps: [],
    result: [],
    guideQueries: [],
    status: 'streaming',
  }
}

function isReplyMessage(message: ChatMessage): message is AgentReplyMessage {
  return message.role === 'assistant'
}

function getColumns(rows: ResultRow[]): string[] {
  if (rows.length === 0) {
    return []
  }

  return Object.keys(rows[0] ?? {})
}

function shouldShowResult(rows: ResultRow[]): boolean {
  return hasDisplayableResult(rows)
}

function applyGuideQuery(query: string) {
  inputValue.value = query
}

async function scrollToBottom() {
  await nextTick()

  if (!scrollContainer.value) {
    return
  }

  scrollContainer.value.scrollTo({
    top: scrollContainer.value.scrollHeight,
    behavior: 'smooth',
  })
}

async function submitQuery() {
  const query = inputValue.value.trim()

  if (!query) {
    return
  }

  if (activeController) {
    activeController.abort()
    activeController = null
  }

  const userMessage = {
    id: crypto.randomUUID(),
    role: 'user' as const,
    content: query,
  }
  const replyMessage = createReplyMessage()

  messages.value.push(userMessage, replyMessage)
  inputValue.value = ''
  isLoading.value = true
  await scrollToBottom()

  const controller = new AbortController()
  activeController = controller

  try {
    await streamAgentQuery(query, {
      signal: controller.signal,
      onStep: (nextMessage) => {
        const index = messages.value.findIndex((item) => item.id === replyMessage.id)

        if (index === -1) {
          return
        }

        messages.value[index] = {
          ...nextMessage,
          id: replyMessage.id,
        }
        void scrollToBottom()
      },
    })

    const index = messages.value.findIndex((item) => item.id === replyMessage.id)
    const currentMessage = index >= 0 ? messages.value[index] : undefined
    if (currentMessage && isReplyMessage(currentMessage) && currentMessage.status === 'streaming') {
      messages.value[index] = {
        ...currentMessage,
        status: 'success',
      }
    }
  } catch (error) {
    if (controller.signal.aborted) {
      const index = messages.value.findIndex((item) => item.id === replyMessage.id)
      const currentMessage = index >= 0 ? messages.value[index] : undefined
      if (currentMessage && isReplyMessage(currentMessage)) {
        messages.value[index] = {
          ...currentMessage,
          status: 'error',
          errorMessage: '本轮查询已被新的提问中止。',
        }
      }
    } else {
      const index = messages.value.findIndex((item) => item.id === replyMessage.id)
      const currentMessage = index >= 0 ? messages.value[index] : undefined
      if (currentMessage && isReplyMessage(currentMessage)) {
        messages.value[index] = {
          ...currentMessage,
          status: 'error',
          errorMessage: toErrorMessage(error),
        }
      }
    }
  } finally {
    if (activeController === controller) {
      activeController = null
      isLoading.value = false
    }

    await scrollToBottom()
  }
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void submitQuery()
  }
}

onBeforeUnmount(() => {
  activeController?.abort()
})
</script>

<template>
  <main
    class="relative min-h-screen overflow-hidden px-4 py-8 text-[14px] text-slate-900 sm:px-6 lg:px-8"
  >
    <div class="pointer-events-none absolute inset-0 overflow-hidden">
      <div
        class="absolute left-[-8rem] top-[-6rem] h-72 w-72 rounded-full bg-sky-200/50 blur-3xl"
      />
      <div
        class="absolute bottom-[-8rem] right-[-4rem] h-80 w-80 rounded-full bg-emerald-200/50 blur-3xl"
      />
    </div>

    <section
      class="relative mx-auto flex min-h-[calc(100vh-4rem)] w-full max-w-6xl items-center justify-center"
    >
      <div
        class="flex h-[82vh] w-full max-w-5xl flex-col overflow-hidden rounded-[32px] border border-white/70 bg-white/82 shadow-[0_30px_90px_rgba(15,23,42,0.12)] backdrop-blur-xl"
      >
        <header class="border-b border-slate-200/70 px-6 py-6 sm:px-8">
          <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div class="space-y-2">
              <p class="text-xs font-semibold uppercase tracking-[0.35em] text-sky-600">
                Text to SQL
              </p>
              <h1 class="text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl">
                智能数据问答工作台
              </h1>
            </div>

            <div
              class="inline-flex items-center gap-3 rounded-full border border-slate-200 bg-slate-50/90 px-4 py-2 text-xs text-slate-500 sm:text-sm"
            >
              <span
                class="inline-flex h-2.5 w-2.5 rounded-full"
                :class="isLoading ? 'animate-pulse bg-amber-400' : 'bg-emerald-500'"
              />
              {{ isLoading ? '执行中' : '等待提问' }}
            </div>
          </div>
        </header>

        <div
          ref="scrollContainer"
          class="flex-1 space-y-6 overflow-y-auto bg-[linear-gradient(180deg,rgba(255,255,255,0.48),rgba(241,245,249,0.72))] px-4 py-6 sm:px-6 lg:px-8"
        >
          <div
            v-for="message in messages"
            :key="message.id"
            class="mx-auto flex w-full max-w-4xl"
            :class="message.role === 'user' ? 'justify-end' : 'justify-start'"
          >
            <article
              v-if="message.role === 'user'"
              class="max-w-2xl rounded-[26px] rounded-br-md bg-slate-900 px-5 py-4 text-xs leading-6 text-white shadow-[0_18px_40px_rgba(15,23,42,0.16)] sm:text-sm"
            >
              {{ message.content }}
            </article>

            <article
              v-else
              class="w-full max-w-3xl rounded-[28px] rounded-bl-md border border-white/75 bg-white/92 px-5 py-5 shadow-[0_18px_40px_rgba(148,163,184,0.16)] sm:px-6"
            >
              <div class="mb-4 flex items-center justify-between gap-4">
                <div>
                  <p class="text-xs font-semibold uppercase tracking-[0.28em] text-slate-400">
                    Agent Status
                  </p>
                  <h3 class="mt-1 text-base font-semibold text-slate-900">执行链路</h3>
                </div>

                <span
                  class="inline-flex items-center rounded-full px-3 py-1 text-xs font-semibold"
                  :class="
                    message.status === 'success'
                      ? 'bg-emerald-50 text-emerald-600'
                      : message.status === 'error'
                        ? 'bg-rose-50 text-rose-600'
                        : 'bg-amber-50 text-amber-600'
                  "
                >
                  {{
                    message.status === 'success'
                      ? '已完成'
                      : message.status === 'error'
                        ? '已中断'
                        : '处理中'
                  }}
                </span>
              </div>

              <div class="space-y-3">
                <div
                  v-for="step in message.steps"
                  :key="step.step"
                  class="flex items-center gap-3 rounded-2xl border border-slate-100 bg-slate-50/85 px-4 py-3"
                >
                  <span
                    v-if="step.status === 'running'"
                    class="h-5 w-5 rounded-full border-2 border-sky-200 border-t-sky-500 animate-spin"
                  />
                  <span
                    v-else-if="step.status === 'success'"
                    class="inline-flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500 text-xs font-bold text-white"
                  >
                    ✓
                  </span>
                  <span
                    v-else
                    class="inline-flex h-5 w-5 items-center justify-center rounded-full bg-rose-500 text-xs font-bold text-white"
                  >
                    !
                  </span>

                  <span class="text-xs font-medium text-slate-700 sm:text-sm">{{ step.step }}</span>
                </div>
              </div>

              <p
                v-if="message.status === 'error' && message.errorMessage"
                class="mt-4 text-xs text-rose-500 sm:text-sm"
              >
                {{ message.errorMessage }}
              </p>

              <section
                v-if="message.status === 'success' && message.guideQueries.length > 0"
                class="mt-6 rounded-3xl border border-sky-100 bg-sky-50/70 p-4 sm:p-5"
              >
                <div class="mb-3">
                  <p class="mt-1 text-xs text-sky-600/80 sm:text-sm">
                    你好,我是Text2SQL,能将您的需求转换为SQL语句进行查询,您可以像下面一样提问
                  </p>
                </div>

                <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <button
                    v-for="guideQuery in message.guideQueries"
                    :key="`${message.id}-${guideQuery}`"
                    type="button"
                    class="rounded-2xl border border-sky-200 bg-white px-4 py-3 text-left text-xs leading-6 text-slate-700 transition hover:border-sky-300 hover:bg-sky-50 hover:text-sky-700 sm:text-sm"
                    @click="applyGuideQuery(guideQuery)"
                  >
                    {{ guideQuery }}
                  </button>
                </div>
              </section>

              <section
                v-if="message.status === 'success' && !shouldShowResult(message.result)"
                class="mt-6 rounded-3xl border border-slate-200 bg-slate-50 px-4 py-4 text-xs text-slate-600 sm:px-5 sm:text-sm"
              >
                {{ emptyResultMessage }}
              </section>

              <section
                v-else-if="shouldShowResult(message.result)"
                class="mt-6 overflow-hidden rounded-3xl border border-slate-200"
              >
                <div class="border-b border-slate-200 bg-slate-50 px-4 py-3">
                  <h4 class="text-xs font-semibold text-slate-700 sm:text-sm">查询结果</h4>
                </div>

                <div class="overflow-x-auto">
                  <table class="min-w-full divide-y divide-slate-200 text-left text-xs sm:text-sm">
                    <thead class="bg-slate-900 text-slate-100">
                      <tr>
                        <th
                          v-for="column in getColumns(message.result)"
                          :key="column"
                          class="whitespace-nowrap px-4 py-3 font-medium tracking-wide"
                        >
                          {{ column }}
                        </th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100 bg-white">
                      <tr
                        v-for="(row, index) in message.result"
                        :key="`${message.id}-${index}`"
                        class="hover:bg-slate-50/80"
                      >
                        <td
                          v-for="column in getColumns(message.result)"
                          :key="`${message.id}-${index}-${column}`"
                          class="whitespace-nowrap px-4 py-3 text-slate-600"
                        >
                          {{ formatResultValue(row[column]) }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </section>
            </article>
          </div>
        </div>

        <footer class="border-t border-slate-200/70 bg-white/95 px-4 py-4 sm:px-6 lg:px-8">
          <form class="mx-auto flex w-full max-w-4xl flex-col gap-3" @submit.prevent="submitQuery">
            <label for="query-input" class="text-xs font-medium text-slate-600 sm:text-sm">
              输入你的业务问题，按 Enter 发送，Shift + Enter 换行
            </label>

            <div
              class="flex flex-col gap-3 rounded-[28px] border border-slate-200 bg-slate-50/85 p-3 shadow-inner shadow-slate-200/30 sm:flex-row sm:items-end"
            >
              <textarea
                id="query-input"
                v-model="inputValue"
                rows="3"
                class="min-h-[80px] flex-1 resize-none rounded-[22px] border border-white bg-white px-4 py-3 text-xs leading-6 text-slate-700 outline-none ring-0 transition placeholder:text-slate-400 focus:border-sky-300 sm:text-sm"
                placeholder="请输入想查询的数据问题，例如：销量最高的商品有哪些？"
                @keydown="handleKeydown"
              />

              <button
                type="submit"
                class="inline-flex h-11 items-center justify-center rounded-2xl bg-slate-900 px-6 text-xs font-semibold text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300 sm:min-w-[112px] sm:text-sm"
                :disabled="!canSend"
              >
                {{ isLoading ? '重新提问' : '发送查询' }}
              </button>
            </div>
          </form>
        </footer>
      </div>
    </section>
  </main>
</template>
