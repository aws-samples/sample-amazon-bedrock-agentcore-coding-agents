/**
 * the code app runtime injection.
 *
 * The shared Code source (this package) is consumed by TWO shells that resolve
 * backend URLs DIFFERENTLY:
 *
 *   - Standalone the-console (web): the orchestrator is same-origin via a
 *     Cloudflare Service Binding, so getOrchestratorUrl() === '' and fetches hit
 *     `/api/*` directly. Relay URL comes from web's app.config env table.
 *
 *   - In-shell the code app (apps/web; web + Electron + Capacitor): the
 *     orchestrator has no public host and is reached through the the app
 *     Worker's `/api` proxy. The base differs per runtime (same-origin on
 *     app.example.com, absolute app host on desktop/mobile). Relay URL comes from the
 *     the shell config.
 *
 * Rather than import a fixed `config.ts` (which is exactly what diverged between
 * the two copies), the shared source reads these two functions from a runtime
 * the consumer installs ONCE at startup via configureCodeRuntime(). This is the
 * single seam that lets one source serve both shells.
 */

export interface CodeRuntime {
  /**
   * Base URL for the orchestrator API. fetch sites build `${base}${path}` where
   * path already starts with `/api` (e.g. `/api/tasks`). Return '' for
   * same-origin (Service Binding) or an absolute base ending without a trailing
   * slash (e.g. `https://app.example.com/api`).
   */
  getOrchestratorUrl: () => string;
  /** Base URL for the relay (auth + usage), no trailing slash. */
  getRelayUrl: () => string;
}

let runtime: CodeRuntime | null = null;

/** Install the runtime. Call once before any Code component mounts. */
export function configureCodeRuntime(rt: CodeRuntime): void {
  runtime = rt;
}

function requireRuntime(): CodeRuntime {
  if (!runtime) {
    throw new Error(
      '@foxl/code: runtime not configured. Call configureCodeRuntime({ getOrchestratorUrl, getRelayUrl }) at app startup before mounting Code.',
    );
  }
  return runtime;
}

export function getOrchestratorUrl(): string {
  return requireRuntime().getOrchestratorUrl();
}

export function getRelayUrl(): string {
  return requireRuntime().getRelayUrl();
}
