export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export class SessionExpiredError extends ApiError {
  constructor() {
    super(401, 'Your session has expired. Sign in again, then return to this tab and choose Check sign-in.');
  }
}

export class SessionChangedError extends Error {
  constructor() {
    super('Your sign-in changed while this request was in progress. Check its outcome before trying again.');
  }
}

export interface AuthUser {
  authenticated: boolean;
  mode?: 'cognito' | 'password' | 'local';
  login_url?: string;
  user_id?: string;
  email?: string;
  name?: string;
  groups?: string[];
}

interface SessionState {
  user: AuthUser | null;
  expired: boolean;
  checking: boolean;
  loginUrl: string | null;
  error: string;
  revision: number;
}

async function readIdentity(fetcher: typeof fetch): Promise<AuthUser> {
  const response = await fetcher('/api/auth/me', { cache: 'no-store', headers: { accept: 'application/json' } });
  if (!response.ok && response.status !== 401) {
    throw new ApiError(response.status, `Could not check your sign-in (${response.status}). Try again.`);
  }
  const user: AuthUser | null = await response.json().catch(() => null);
  if (!user || typeof user.authenticated !== 'boolean') {
    throw new ApiError(response.status, 'The host returned an invalid sign-in response. Choose Check sign-in to try again.');
  }
  if (response.ok && !user.authenticated && user.login_url === undefined
      && user.mode !== 'local' && user.mode !== 'password') {
    throw new ApiError(response.status, 'The host did not verify a sign-in or local access. Choose Check sign-in to try again.');
  }
  // Only a same-origin path supplied by the host may open a sign-in page.
  if ((response.status === 401 || user.login_url !== undefined)
      && (user.authenticated || typeof user.login_url !== 'string' || !/^\/(?![\\/])[^\\\s]*$/.test(user.login_url))) {
    throw new ApiError(401, 'Your session has expired, but the host did not return a valid sign-in address. Choose Check sign-in to try again.');
  }
  return user;
}

/**
 * Keep recovery in this tab. A 401 never navigates away, retries an operation, or
 * clears a draft. Only an explicit check after sign-in resumes API requests.
 */
export function createAuthSession(fetcher: typeof fetch) {
  let state: SessionState = { user: null, expired: false, checking: true, loginUrl: null, error: '', revision: 0 };
  let generation = 0;
  let pending: { generation: number; id: symbol; promise: Promise<void> } | null = null;
  const listeners = new Set<() => void>();
  const update = (next: Partial<SessionState>) => {
    state = { ...state, ...next };
    listeners.forEach(listener => listener());
  };
  const expire = (loginUrl = state.loginUrl) => {
    if (!state.expired) generation++;
    update({ expired: true, user: null, loginUrl, error: '' });
  };

  function check(resume = true): Promise<void> {
    const started = generation;
    if (pending?.generation === started) return pending.promise;
    const id = Symbol();
    update({ checking: true, error: '' });
    const promise = (async () => {
      try {
        const user = await readIdentity(fetcher);
        // An older identity response cannot undo a newer 401 or sign-in check.
        if (started !== generation) return;
        if (user.login_url) {
          expire(user.login_url);
        } else if (!state.expired || resume) {
          const recovered = state.expired;
          if (recovered) generation++;
          update({ user, expired: false, loginUrl: null, revision: state.revision + Number(recovered) });
        }
      } catch (error) {
        if (started !== generation) return;
        if (error instanceof ApiError && error.status === 401) expire();
        update({ error: error instanceof Error ? error.message : 'Could not check your sign-in. Try again.' });
      } finally {
        if (pending?.id === id) {
          pending = null;
          update({ checking: false });
        }
      }
    })();
    pending = { generation: started, id, promise };
    return promise;
  }

  async function request(path: string, init?: RequestInit): Promise<Response> {
    if (state.expired) throw new SessionExpiredError();
    const started = generation;
    const response = await fetcher(path, init);
    if (response.status === 401) {
      if (started === generation) {
        expire();
        // Resolve this host's Cognito/password login destination once. Even if
        // another tab has signed in, this read alone does not resume operations.
        void check(false);
      }
      throw new SessionExpiredError();
    }
    if (started !== generation || state.expired) throw new SessionChangedError();
    return response;
  }

  return {
    request, check,
    getAuthMe: () => readIdentity(fetcher),
    getSnapshot: () => state,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}

export const authSession = createAuthSession((input, init) => fetch(input, init));
export const apiFetch = authSession.request;
export const getAuthMe = authSession.getAuthMe;
