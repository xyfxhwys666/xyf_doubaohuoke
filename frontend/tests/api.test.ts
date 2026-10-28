import assert from 'node:assert/strict'
import test from 'node:test'

import { api } from '../src/api.ts'

test('natural-language task sends the Doubao web chat option', async () => {
  let requestBody: Record<string, unknown> | undefined
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (_input, init) => {
    requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>
    return new Response('{}', { status: 200 })
  }

  try {
    await api.createNaturalLanguageTask('查深圳软装公司30家', ['doubao'], {
      continuousMode: false,
      verifiedAutoDeliver: false,
      useDoubaoWebChat: true,
    })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(requestBody?.use_doubao_web_chat, true)
})
