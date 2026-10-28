/**
 * 评测解读报告（`Result.report`）是受限子集的 Markdown 正文：
 * 只有小标题（`#` / `##`）、有序与无序列表、加粗、行内代码，
 * 不含代码块围栏与表格（提示词已禁用，见 api_docs.md「评测结果报告是 Markdown 正文」）。
 *
 * 因此这里不做通用 Markdown 解析，只渲染这个子集：
 * 先转义 HTML 再转换，报告里的尖括号不会变成标签。
 */

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** 行内格式：先切出行内代码，再处理加粗，避免代码里的 `**` 被当成加粗 */
function inline(text: string): string {
  return text
    .split(/(`[^`]*`)/)
    .map(part =>
      part.length >= 2 && part.startsWith('`') && part.endsWith('`')
        ? `<code>${part.slice(1, -1)}</code>`
        : part.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>'),
    )
    .join('')
}

/** 把报告正文渲染成 HTML；调用方用 v-html 挂上即可 */
export function renderMarkdown(source: string): string {
  const html: string[] = []
  const paragraph: string[] = []
  let listType: 'ul' | 'ol' | null = null

  const flushParagraph = (): void => {
    if (!paragraph.length) return
    html.push(`<p>${paragraph.map(inline).join('<br>')}</p>`)
    paragraph.length = 0
  }

  const closeList = (): void => {
    if (!listType) return
    html.push(`</${listType}>`)
    listType = null
  }

  const openList = (type: 'ul' | 'ol'): void => {
    if (listType === type) return
    closeList()
    html.push(`<${type}>`)
    listType = type
  }

  for (const rawLine of escapeHtml(source).split(/\r?\n/)) {
    const line = rawLine.trim()

    if (!line) {
      flushParagraph()
      closeList()
      continue
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      flushParagraph()
      closeList()
      const [, hashes = '#', title = ''] = heading
      const level = Math.min(hashes.length + 3, 6)
      html.push(`<h${level}>${inline(title)}</h${level}>`)
      continue
    }

    const bullet = /^[-*+]\s+(.*)$/.exec(line)
    if (bullet) {
      openList('ul')
      html.push(`<li>${inline(bullet[1] ?? '')}</li>`)
      continue
    }

    const ordered = /^\d+[.)]\s+(.*)$/.exec(line)
    if (ordered) {
      openList('ol')
      html.push(`<li>${inline(ordered[1] ?? '')}</li>`)
      continue
    }

    closeList()
    paragraph.push(line)
  }

  flushParagraph()
  closeList()

  return html.join('\n')
}
