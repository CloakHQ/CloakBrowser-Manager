import { render, type RenderOptions } from "@testing-library/react";
import type { ReactElement } from "react";
import { ConfirmProvider } from "../components/ui/confirm";
import { ToastProvider } from "../components/ui/toast";

/** Render inside the app-wide toast + confirm providers. */
export function renderWithProviders(ui: ReactElement, options?: Omit<RenderOptions, "wrapper">) {
  return render(ui, {
    wrapper: ({ children }) => (
      <ToastProvider>
        <ConfirmProvider>{children}</ConfirmProvider>
      </ToastProvider>
    ),
    ...options,
  });
}
