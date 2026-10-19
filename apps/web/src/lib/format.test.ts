import { describe, it, expect } from 'vitest';
import { formatDate, formatDurationMs, formatDurationSeconds } from './format';

describe('format utilities', () => {
  it('preserves formatDate', () => {
    const formatted = formatDate('2026-10-16T12:00:00Z');
    expect(formatted).toBeDefined();
    expect(typeof formatted).toBe('string');
  });

  it('formatDurationMs handles unknown, zero, ms, and seconds', () => {
    expect(formatDurationMs(null)).toBe('Unknown');
    expect(formatDurationMs(undefined)).toBe('Unknown');
    expect(formatDurationMs(-1)).toBe('Unknown');
    expect(formatDurationMs(0)).toBe('0ms');
    expect(formatDurationMs(450)).toBe('450ms');
    expect(formatDurationMs(1200)).toBe('1.2s');
    expect(formatDurationMs(60000)).toBe('1m');
    expect(formatDurationMs(90000)).toBe('1m 30s');
    expect(formatDurationMs(3600000)).toBe('1h');
  });

  it('formatDurationSeconds handles unknown, zero, seconds, minutes, hours, days', () => {
    expect(formatDurationSeconds(null)).toBe('Unknown');
    expect(formatDurationSeconds(undefined)).toBe('Unknown');
    expect(formatDurationSeconds(-5)).toBe('Unknown');
    expect(formatDurationSeconds(0)).toBe('0s');
    expect(formatDurationSeconds(45)).toBe('45s');
    expect(formatDurationSeconds(120)).toBe('2m');
    expect(formatDurationSeconds(125)).toBe('2m 5s');
    expect(formatDurationSeconds(3600)).toBe('1h');
    expect(formatDurationSeconds(3660)).toBe('1h 1m');
    expect(formatDurationSeconds(86400)).toBe('1d');
    expect(formatDurationSeconds(90000)).toBe('1d 1h');
  });
});
