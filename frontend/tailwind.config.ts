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
        // Every colour is a CSS variable defined in globals.css, so the same
        // semantic names resolve differently in the light and dark themes.
        surface: {
          0: "rgb(var(--surface-0) / <alpha-value>)",
          1: "rgb(var(--surface-1) / <alpha-value>)",
          2: "rgb(var(--surface-2) / <alpha-value>)",
          3: "rgb(var(--surface-3) / <alpha-value>)",
          4: "rgb(var(--surface-4) / <alpha-value>)",
        },
        fg: {
          DEFAULT: "rgb(var(--fg) / <alpha-value>)",
          secondary: "rgb(var(--fg-secondary) / <alpha-value>)",
          muted: "rgb(var(--fg-muted) / <alpha-value>)",
        },
        line: {
          DEFAULT: "rgb(var(--line) / <alpha-value>)",
          strong: "rgb(var(--line-strong) / <alpha-value>)",
        },
        brand: {
          DEFAULT: "rgb(var(--brand) / <alpha-value>)",
          hover: "rgb(var(--brand-hover) / <alpha-value>)",
          // Gradient end stop.
          deep: "rgb(var(--brand-deep) / <alpha-value>)",
          // Accent text/icons: same hue as `brand`, lightened in dark so it
          // clears 4.5:1 on the surfaces it sits on.
          fg: "rgb(var(--brand-fg) / <alpha-value>)",
          subtle: "rgb(var(--brand) / var(--brand-subtle-alpha))",
        },
        success: "rgb(var(--success) / <alpha-value>)",
        warning: "rgb(var(--warning) / <alpha-value>)",
        danger: "rgb(var(--danger) / <alpha-value>)",
        info: "rgb(var(--info) / <alpha-value>)",
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
        // Per-theme in globals.css: a dark theme needs drop shadows, a light
        // one needs hairlines instead.
        card: "var(--shadow-card)",
        elevated: "var(--shadow-elevated)",
        popover: "var(--shadow-popover)",
        glow: "var(--shadow-glow)",
      },
      spacing: {
        rail: "3.25rem",
        sidebar: "16.5rem",
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
