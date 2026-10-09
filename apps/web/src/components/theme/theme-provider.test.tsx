import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { APPEARANCE_STORAGE_KEY } from '@/lib/theme';
import { AppearanceControl, ThemeProvider } from './theme-provider';

let systemDark = false;
let systemListeners: Array<() => void> = [];
const media = {
  get matches() { return systemDark; },
  media: '(prefers-color-scheme: dark)',
  onchange: null,
  addEventListener: (_type: string, listener: () => void) => { systemListeners.push(listener); },
  removeEventListener: (_type: string, listener: () => void) => { systemListeners = systemListeners.filter(item => item !== listener); },
  addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(() => true),
} as unknown as MediaQueryList;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.appearance;
  systemDark = false;
  systemListeners = [];
});

function renderControl() {
  return render(<ThemeProvider><AppearanceControl /></ThemeProvider>);
}

test('control starts from a saved choice and changes the document theme and saved value', async () => {
  systemDark = true;
  localStorage.setItem(APPEARANCE_STORAGE_KEY, 'light');
  vi.stubGlobal('matchMedia', vi.fn(() => media));
  const user = userEvent.setup();
  renderControl();
  const control = screen.getByRole('combobox', { name: 'Appearance' });
  expect(control).toHaveValue('light');
  expect(document.documentElement.dataset.theme).toBe('light');
  await user.selectOptions(control, 'dark');
  expect(control).toHaveValue('dark');
  expect(document.documentElement.dataset.theme).toBe('dark');
  expect(localStorage.getItem(APPEARANCE_STORAGE_KEY)).toBe('dark');
});

test('System follows OS changes while explicit preference remains fixed', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => media));
  const user = userEvent.setup();
  renderControl();
  const control = screen.getByRole('combobox', { name: 'Appearance' });
  expect(control).toHaveValue('system');
  systemDark = true;
  systemListeners.forEach(listener => listener());
  expect(document.documentElement.dataset.theme).toBe('dark');
  await user.selectOptions(control, 'light');
  systemDark = false;
  systemListeners.forEach(listener => listener());
  expect(document.documentElement.dataset.theme).toBe('light');
  systemDark = true;
  systemListeners.forEach(listener => listener());
  expect(document.documentElement.dataset.theme).toBe('light');
  await user.selectOptions(control, 'dark');
  systemDark = false;
  systemListeners.forEach(listener => listener());
  expect(document.documentElement.dataset.theme).toBe('dark');
  await user.selectOptions(control, 'system');
  expect(document.documentElement.dataset.theme).toBe('light');
  systemDark = true;
  systemListeners.forEach(listener => listener());
  expect(document.documentElement.dataset.theme).toBe('dark');
});

test('storage events synchronize appearance choices from another tab', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => media));
  renderControl();
  localStorage.setItem(APPEARANCE_STORAGE_KEY, 'dark');
  await act(async () => { window.dispatchEvent(new StorageEvent('storage', { key: APPEARANCE_STORAGE_KEY, newValue: 'dark' })); });
  expect(screen.getByRole('combobox', { name: 'Appearance' })).toHaveValue('dark');
  expect(document.documentElement.dataset.theme).toBe('dark');
});

test('denied localStorage does not prevent live theme changes', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => media));
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  const user = userEvent.setup();
  renderControl();
  const control = screen.getByRole('combobox', { name: 'Appearance' });
  expect(control).toHaveValue('system');
  await user.selectOptions(control, 'dark');
  expect(document.documentElement.dataset.theme).toBe('dark');
  expect(control).toHaveValue('dark');
});

test('removing or clearing a saved appearance resumes the current System theme', async () => {
  systemDark = true;
  vi.stubGlobal('matchMedia', vi.fn(() => media));
  localStorage.setItem(APPEARANCE_STORAGE_KEY, 'light');
  renderControl();
  const control = screen.getByRole('combobox', { name: 'Appearance' });
  expect(document.documentElement.dataset.theme).toBe('light');
  localStorage.removeItem(APPEARANCE_STORAGE_KEY);
  await act(async () => { window.dispatchEvent(new StorageEvent('storage', { key: APPEARANCE_STORAGE_KEY })); });
  expect(control).toHaveValue('system');
  expect(document.documentElement.dataset.theme).toBe('dark');
  localStorage.setItem(APPEARANCE_STORAGE_KEY, 'light');
  await act(async () => { window.dispatchEvent(new StorageEvent('storage', { key: APPEARANCE_STORAGE_KEY })); });
  expect(control).toHaveValue('light');
  localStorage.clear();
  await act(async () => { window.dispatchEvent(new StorageEvent('storage', { key: null })); });
  expect(control).toHaveValue('system');
  expect(document.documentElement.dataset.theme).toBe('dark');
});

test('unmount releases OS and storage subscriptions', () => {
  vi.stubGlobal('matchMedia', vi.fn(() => media));
  const removeListener = vi.spyOn(window, 'removeEventListener');
  const { unmount } = renderControl();
  expect(systemListeners).toHaveLength(1);
  unmount();
  expect(systemListeners).toHaveLength(0);
  expect(removeListener).toHaveBeenCalledWith('storage', expect.any(Function));
  localStorage.setItem(APPEARANCE_STORAGE_KEY, 'dark');
  window.dispatchEvent(new StorageEvent('storage', { key: APPEARANCE_STORAGE_KEY }));
  expect(document.documentElement.dataset.theme).toBe('light');
});

test('a throwing localStorage getter preserves System mode and manual switching', async () => {
  systemDark = true;
  vi.stubGlobal('matchMedia', vi.fn(() => media));
  vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new DOMException('denied', 'SecurityError'); });
  const user = userEvent.setup();
  renderControl();
  const control = screen.getByRole('combobox', { name: 'Appearance' });
  expect(control).toHaveValue('system');
  expect(document.documentElement.dataset.theme).toBe('dark');
  await user.selectOptions(control, 'light');
  expect(control).toHaveValue('light');
  expect(document.documentElement.dataset.theme).toBe('light');
});
