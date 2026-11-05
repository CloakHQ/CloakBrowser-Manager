import type { Config } from "tailwindcss";

/**
 * Design tokens for the Custom fork.
 *
 * One semantic palette: surface-* for backgrounds, fg for text, line for
 * borders, brand for the accent colour, and success/warning/danger/info for
 * state badges, where a colour must never be the only signal. The legacy
 * `border`/`accent` aliases were exact duplicates of `line`/`brand` and have
 * been removed — use the semantic names.
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        surface: {
          0: "#080B11",
          1: "#0E131D",
          2: "#141B29",
          3: "#1B2436",
          4: "#243047",
        },
        fg: {
          DEFAULT: "#F1F5F9",
          secondary: "#94A3B8",
          muted: "#64748B",
        },
        line: {
          DEFAULT: "#1E2738",
          strong: "#2E3B54",
        },
        brand: {
          DEFAULT: "#6366F1",
          hover: "#818CF8",
          // Gradient end stop (was the raw indigo-600).
          deep: "#4F46E5",
          subtle: "rgba(99, 102, 241, 0.14)",
        },
        success: "#10B981",
        warning: "#F59E0B",
        danger: "#F43F5E",
        info: "#38BDF8",
      },
      borderRadius: {
        sm: "6px",
        md: "8px",
        lg: "12px",
        xl: "16px",
        "2xl": "20px",
      },
      fontSize: {
        "2xs": ["11px", "15px"],
        xs: ["12px", "16px"],
        sm: ["13px", "19px"],
        base: ["14px", "20px"],
        lg: ["16px", "24px"],
        xl: ["20px", "28px"],
        "2xl": ["24px", "32px"],
      },
      boxShadow: {
        card: "0 1px 3px rgba(0, 0, 0, 0.35), 0 1px 2px rgba(0, 0, 0, 0.25), inset 0 1px 0 rgba(255, 255, 255, 0.04)",
        elevated: "0 8px 24px -4px rgba(0, 0, 0, 0.5), 0 2px 6px -1px rgba(0, 0, 0, 0.3), inset 0 1px 0 rgba(255, 255, 255, 0.05)",
        popover: "0 20px 48px -8px rgba(0, 0, 0, 0.65), 0 0 1px 1px rgba(255, 255, 255, 0.06)",
        glow: "0 0 20px -4px rgba(99, 102, 241, 0.45)",
      },
      spacing: {
        rail: "3.25rem",
        sidebar: "18.5rem",
        topbar: "3.5rem",
        statusbar: "1.875rem",
      },
      fontFamily: {
        sans: [
          "Inter Variable",
          "Inter",
          "Segoe UI Variable",
          "Segoe UI",
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
        mono: ["JetBrains Mono", "Cascadia Mono", "Consolas", "ui-monospace", "monospace"],
      },
      zIndex: {
        popover: "40",
        modal: "50",
        toast: "60",
      },
    },
  },
  plugins: [],
} satisfies Config;
