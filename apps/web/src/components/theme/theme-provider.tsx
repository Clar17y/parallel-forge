'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { APPEARANCE_STORAGE_KEY, normalizeAppearance, readAppearance, resolveTheme, writeAppearance, type Appearance } from '@/lib/theme';

type ThemeContextValue = { appearance: Appearance; setAppearance: (appearance: Appearance) => void };
const ThemeContext = createContext<ThemeContextValue | null>(null);

function getSavedAppearance(): Appearance {
  return readAppearance({ getItem: key => window.localStorage.getItem(key) });
}

function saveAppearance(appearance: Appearance): void {
  writeAppearance({ setItem: (key, value) => window.localStorage.setItem(key, value) }, appearance);
}

function colorSchemeMedia(): Pick<MediaQueryList, 'matches' | 'addEventListener' | 'removeEventListener'> {
  return typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)')
    : { matches: false, addEventListener: () => undefined, removeEventListener: () => undefined };
}

function applyAppearance(appearance: Appearance, systemIsDark: boolean): void {
  const root = document.documentElement;
  root.dataset.appearance = appearance;
  root.dataset.theme = resolveTheme(appearance, systemIsDark);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [appearance, setCurrentAppearance] = useState<Appearance>('system');

  useEffect(() => {
    const media = colorSchemeMedia();
    const sync = (next: Appearance) => {
      applyAppearance(next, media.matches);
      setCurrentAppearance(next);
    };
    sync(getSavedAppearance());

    const onSystemChange = () => {
      if (normalizeAppearance(document.documentElement.dataset.appearance) === 'system') {
        applyAppearance('system', media.matches);
      }
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === APPEARANCE_STORAGE_KEY || event.key === null) sync(getSavedAppearance());
    };

    media.addEventListener('change', onSystemChange);
    window.addEventListener('storage', onStorage);
    return () => {
      media.removeEventListener('change', onSystemChange);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  const setAppearance = useCallback((next: Appearance) => {
    const normalized = normalizeAppearance(next);
    saveAppearance(normalized);
    applyAppearance(normalized, colorSchemeMedia().matches);
    setCurrentAppearance(normalized);
  }, []);
  const value = useMemo(() => ({ appearance, setAppearance }), [appearance, setAppearance]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function AppearanceControl() {
  const context = useContext(ThemeContext);
  if (!context) return null;
  return <label className="appearance-control">Appearance
    <select aria-label="Appearance" value={context.appearance} onChange={event => context.setAppearance(normalizeAppearance(event.currentTarget.value))}>
      <option value="light">Light</option>
      <option value="dark">Dark</option>
      <option value="system">System</option>
    </select>
  </label>;
}
