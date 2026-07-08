import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App.tsx';
import { configureCodeRuntime } from '@foxl/code/runtime';
import './styles.css';

// The console is same-origin with its backend: server.py serves this built app
// at / and the stage engines under /api/dev|orchestrator|metrics. The vendored code reads the
// orchestrator base from this runtime ('' means same-origin), so fetch sites
// hit `/api/...` directly. No relay (auth is not used in the workshop console).
configureCodeRuntime({
  getOrchestratorUrl: () => '',
  getRelayUrl: () => '',
});

// Router basename, derived from where this document is actually served.
//
// Behind the workshop's CloudFront + nginx the console lives under `/console/`
// (code-server owns `/`), so nginx routes ONLY `/console/*`, `/api/*`,
// `/assets/*`, `/vendor/*`, `/auth/*` to the console; anything else falls to
// code-server. With no basename, BrowserRouter emitted BARE in-app URLs
// (`/fleets/c/<id>`), so a hard refresh (Cmd-R) or deep link requested a bare
// path -> nginx `location /` -> code-server -> a 401 / its own /login page.
// Anchoring the router at `/console` keeps every in-app URL under the one
// prefix nginx forwards here, so a refresh round-trips back to the SPA.
//
// Detected at runtime (same build serves both): in prod EVERY console URL is
// under `/console`; in local dev (`:5174` / `CONSOLE_DEV` on `:8080`) nothing
// is, so the basename is empty and routes stay at the root. `<meta name=
// "console-base">` can override for any other mount. API calls are absolute
// (`/api/...`) and unaffected; useLocation() returns basename-stripped paths,
// so App.tsx's `pathname.startsWith('/fleets')` checks keep working.
function routerBasename(): string {
  const override = document
    .querySelector('meta[name="console-base"]')
    ?.getAttribute('content');
  if (override) return override.replace(/\/$/, '');
  return window.location.pathname.startsWith('/console') ? '/console' : '';
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter basename={routerBasename()}>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
