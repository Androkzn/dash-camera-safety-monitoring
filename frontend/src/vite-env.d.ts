/**
 * vite-env.d.ts — Ambient TypeScript declarations for the Vite build.
 *
 * WHAT THIS FILE IS
 *   A `.d.ts` file is a "declaration file". It holds TYPES ONLY — no runtime
 *   code is emitted. TypeScript reads it during compilation so your editor
 *   and `tsc` know about global names the actual JS runtime will provide.
 *
 * WHAT THE DIRECTIVE BELOW DOES
 *   The `/// <reference types="vite/client" />` line is a "triple-slash
 *   directive". It tells TypeScript: "also load the type definitions that
 *   ship with the npm package `vite/client`." Those types declare things
 *   like:
 *     - `import.meta.env` (so `import.meta.env.MODE` type-checks).
 *     - `import.meta.env.DEV` / `.PROD` / `.BASE_URL`.
 *     - Module shims so `import logo from "./logo.svg?url"` and
 *       `import raw from "./file.txt?raw"` are recognised.
 *
 * WHY WE NEED IT
 *   Vite injects `import.meta.env.*` at build time, and supports importing
 *   assets/URLs/raw strings with special query suffixes. Without these
 *   ambient types, TypeScript would not know those are valid and would
 *   flag them as errors. This file is generated automatically by
 *   `npm create vite@latest` and should normally be left alone.
 *
 * WHERE IT FITS
 *   Loaded by tsconfig.app.json via its `include` list. Zero bytes are
 *   shipped to the browser; this is purely a compile-time aid.
 */

/// <reference types="vite/client" />
