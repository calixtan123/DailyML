export type LocalRequest = <T>(path: string, options?: RequestInit) => Promise<T>
export type LocalFileRequest = (path: string, options?: RequestInit) => Promise<Blob>

/** Preserve structured local errors so the draft editor can show a conflicting save. */
export class LocalApiError extends Error {
  constructor(readonly status: number, readonly payload: { detail?: unknown; saved?: unknown } | null) {
    super(typeof payload?.detail === 'string'
      ? payload.detail
      : 'Daily ML Practice could not complete the request.')
  }
}

async function responseData<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>

  const payload = await response.json().catch(() => null)
  throw new LocalApiError(response.status, payload)
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

/** Download a protected local file while reusing the current CSRF session. */
export const requestFile: LocalFileRequest = async (path: string, options: RequestInit = {}) => {
  const send = async () => {
    const token = await getSession()
    const headers = new Headers(options.headers)
    if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', token)
    return fetch(path, { ...options, headers, cache: 'no-store', credentials: 'same-origin' })
  }
  let response = await send()
  if (response.status === 403) {
    session = undefined
    response = await send()
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new LocalApiError(response.status, payload)
  }
  return response.blob()
}
