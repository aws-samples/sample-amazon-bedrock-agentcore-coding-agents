/// <reference types="vite/client" />

// Vite's `?inline` query suffix forces an asset to resolve to a base64 data URI
// (string) in both dev and build. vite/client declares the bare `*.png`/`*.svg`
// modules but not the query-suffixed forms, so declare them here. Used by the
// agent backend icons to paint with zero network round-trips.
declare module '*?inline' {
  const src: string;
  export default src;
}
