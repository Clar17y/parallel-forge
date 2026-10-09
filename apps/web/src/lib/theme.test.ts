import { describe, expect, test, vi } from 'vitest';
import { APPEARANCE_STORAGE_KEY, normalizeAppearance, readAppearance, resolveTheme, writeAppearance } from './theme';

describe('appearance preference', () => {
  test('normalizes saved values and resolves System from the current OS preference', () => {
    expect(normalizeAppearance('dark')).toBe('dark');
    expect(normalizeAppearance('broken')).toBe('system');
    expect(resolveTheme('system', true)).toBe('dark');
    expect(resolveTheme('system', false)).toBe('light');
    expect(resolveTheme('light', true)).toBe('light');
    expect(resolveTheme('dark', false)).toBe('dark');
  });

  test('reads a valid saved choice and falls back to System for missing, invalid or denied storage', () => {
    expect(readAppearance({ getItem: () => 'dark' })).toBe('dark');
    expect(readAppearance({ getItem: () => null })).toBe('system');
    expect(readAppearance({ getItem: () => 'invalid' })).toBe('system');
    expect(readAppearance({ getItem: () => { throw new Error('denied'); } })).toBe('system');
  });

  test('writes only the appearance preference and reports denied storage', () => {
    const setItem = vi.fn();
    expect(writeAppearance({ setItem }, 'light')).toBe(true);
    expect(setItem).toHaveBeenCalledWith(APPEARANCE_STORAGE_KEY, 'light');
    expect(writeAppearance({ setItem: () => { throw new Error('denied'); } }, 'dark')).toBe(false);
  });
});
