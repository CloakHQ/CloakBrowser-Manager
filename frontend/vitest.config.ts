import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // @cubone/react-file-manager's package.json "main" points at raw,
      // unbuilt src/index.js, which itself imports "./FileManager/FileManager"
      // — a path that only exists in the pre-build source tree, not in what's
      // actually published. `vite build` and the dev server never hit this:
      // both resolve bare-package imports through "module" (the real, working
      // ESM bundle) by default. Vitest's SSR module graph does not share that
      // preference and resolves "main" instead, so this aliases the bare
      // specifier straight at the built file everywhere, tests included.
      "@cubone/react-file-manager/dist/style.css": fileURLToPath(
        new URL(
          "./node_modules/@cubone/react-file-manager/dist/style.css",
          import.meta.url,
        ),
      ),
      "@cubone/react-file-manager": fileURLToPath(
        new URL(
          "./node_modules/@cubone/react-file-manager/dist/react-file-manager.es.js",
          import.meta.url,
        ),
      ),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    coverage: {
      provider: "v8",
      // Default to the whole source tree, not just files a test happened to
      // import — otherwise a component with no test at all (LoginPage was 0%)
      // simply disappears from the report instead of showing up as a hole.
      include: ["src/**"],
      exclude: ["src/main.tsx", "src/styles/**", "src/**/*.test.{ts,tsx}"],
      reporter: ["text", "html"],
      /**
       * Per-file, not global. ProfileForm.tsx (585 lines) and LoginPage.tsx
       * predate this migration at ~3% and 0%, and a global number would either
       * be set low enough to be meaningless or block every change until those
       * are backfilled. These three files are the ones the KasmVNC 1.5
       * migration introduced or rewrote, so they are the ones that must not
       * regress — the surviving mutations in the audit (the degraded overlay,
       * the #186 wheel guard, unbounded createViewerToken/profileStatus) were
       * all in exactly this set.
       */
      thresholds: {
        "src/hooks/useViewerSession.ts": {
          statements: 85, branches: 75, functions: 85, lines: 88,
        },
        // 100% lines. The two uncovered statements (the !wrapperRef.current
        // and !surface early returns) are defensive guards on refs React has
        // already populated by the time the handler or effect runs; faking a
        // null ref to reach them would test the fake, not the component.
        "src/components/ProfileViewer.tsx": {
          statements: 97, branches: 94, functions: 100, lines: 100,
        },
        "src/lib/api.ts": {
          statements: 100, branches: 84, functions: 100, lines: 100,
        },
        // Every branch here is a write the user asked for that silently did
        // not happen — the hook swallows the rejection and `error` is the only
        // signal. It sat at 14% branch with all five catch blocks unexercised.
        "src/hooks/useProfiles.ts": {
          statements: 100, branches: 100, functions: 100, lines: 100,
        },
      },
    },
  },
});
