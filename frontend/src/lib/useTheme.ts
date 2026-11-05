import { useEffect, useState } from "react";
import {
  getThemePreference,
  resolvedTheme,
  setThemePreference,
  subscribeTheme,
  type ResolvedTheme,
  type ThemePreference,
} from "../lib/theme";

/** Current theme preference, re-rendering when the OS or the user changes it. */
export function useTheme(): {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (pref: ThemePreference) => void;
} {
  const [preference, setLocal] = useState<ThemePreference>(getThemePreference);
  const [resolved, setResolved] = useState<ResolvedTheme>(resolvedTheme);

  useEffect(
    () =>
      subscribeTheme(() => {
        setLocal(getThemePreference());
        setResolved(resolvedTheme());
      }),
    [],
  );

  return { preference, resolved, setPreference: setThemePreference };
}
