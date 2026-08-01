import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
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
        "src/components/ProfileViewer.tsx": {
          statements: 67, branches: 74, functions: 58, lines: 67,
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
