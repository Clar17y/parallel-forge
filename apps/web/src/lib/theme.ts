export type Appearance = 'light' | 'dark' | 'system';
export type Theme = Exclude<Appearance, 'system'>;

export const APPEARANCE_STORAGE_KEY = 'forge-appearance';

export function normalizeAppearance(value: unknown): Appearance {
  return value === 'light' || value === 'dark' ? value : 'system';
}

export function resolveTheme(appearance: Appearance, systemIsDark: boolean): Theme {
  return appearance === 'system' ? (systemIsDark ? 'dark' : 'light') : appearance;
}

export function readAppearance(storage: Pick<Storage, 'getItem'>): Appearance {
  try {
    return normalizeAppearance(storage.getItem(APPEARANCE_STORAGE_KEY));
  } catch {
    return 'system';
  }
}

export function writeAppearance(storage: Pick<Storage, 'setItem'>, appearance: Appearance): boolean {
  try {
    storage.setItem(APPEARANCE_STORAGE_KEY, appearance);
    return true;
  } catch {
    return false;
  }
}

// Runs synchronously in the document head so the first rendered surface uses
// the same preference the hydrated provider will reconcile.
export const themeBootstrapScript = `(()=>{const k='${APPEARANCE_STORAGE_KEY}';let p='system';try{const v=localStorage.getItem(k);if(v==='light'||v==='dark')p=v}catch{}let d=false;try{d=matchMedia('(prefers-color-scheme: dark)').matches}catch{}document.documentElement.dataset.appearance=p;document.documentElement.dataset.theme=p==='system'?(d?'dark':'light'):p})()`;
