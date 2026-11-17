export type LocalRequest = <T>(path: string, options?: RequestInit) => Promise<T>

async function responseData<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>

  const payload: { detail?: unknown } | null = await response.json().catch(() => null)
  throw new Error(
    typeof payload?.detail === 'string' ? payload.detail : 'Daily ML Practice could not complete the request.',
  )
}

let session: Promise<string> | undefined

function getSession() {
  session ??= fetch('/api/session', { cache: 'no-store' })
    .then(responseData<{ csrf_token: string }>)
    .then(({ csrf_token }) => csrf_token)
    .catch((error: unknown) => {
      session = undefined
      throw error
    })
  return session
}

export const request: LocalRequest = async <T>(path: string, options: RequestInit = {}) => {
  const send = async () => {
    const token = await getSession()
    const headers = new Headers(options.headers)
    if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', token)
    return fetch(path, { ...options, headers, cache: 'no-store', credentials: 'same-origin' })
  }

  const response = await send()
  if (response.status === 403) {
    // The backend rejects the request before any action when an instance has restarted.
    session = undefined
    return responseData<T>(await send())
  }
  return responseData<T>(response)
}
