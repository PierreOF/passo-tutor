import { afterEach, expect, it, vi } from 'vitest'
import { experimentReviewApi } from './experimentReviewApi'
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })
it('ends stalled requests with an actionable error instead of loading forever', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn((_url: string, options: RequestInit) => new Promise((_resolve, reject) => {
    options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
  })))
  const request = experimentReviewApi.runs()
  const rejected = expect(request).rejects.toThrow('A consulta demorou demais. Tente atualizar a lista.')
  await vi.advanceTimersByTimeAsync(15000)
  await rejected
})
