type ApiError = { message?: string; requestId?: string }

export interface RequestOptions {
  /** Mensagem usada quando a resposta de erro não traz `message`. */
  fallbackMessage: string
  /** Acrescenta ` Código: <requestId>` à mensagem de erro quando a API o devolve. */
  includeRequestId?: boolean
}

function csrfToken(): string | undefined {
  return document.cookie.split('; ').find((item) => item.startsWith('passo_csrf='))?.split('=').slice(1).join('=')
}

/** `fetch` same-origin com token CSRF, corpo JSON e erro legível para a interface. */
export async function request<T>(path: string, init: RequestInit | undefined, options: RequestOptions): Promise<T> {
  const csrf = csrfToken()
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(csrf ? { 'X-CSRF-Token': decodeURIComponent(csrf) } : {}),
      ...init?.headers,
    },
  })
  const payload = await response.json().catch(() => ({})) as T & ApiError
  if (!response.ok) {
    const message = payload.message ?? options.fallbackMessage
    throw new Error(options.includeRequestId && payload.requestId ? `${message} Código: ${payload.requestId}` : message)
  }
  return payload
}
