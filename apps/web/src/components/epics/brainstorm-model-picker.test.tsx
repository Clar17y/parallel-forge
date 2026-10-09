import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { api } from '@/lib/api/client';
import { BrainstormModelPicker } from './brainstorm-model-picker';
import type { BrainstormRoute } from './brainstorm-model-choice';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

const defaultRoute: BrainstormRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-luna', effort: 'medium', auth_mode: 'subscription', billing_mode: 'allowance_only' };
const alternate: BrainstormRoute = { ...defaultRoute, model: 'gpt-6-astra', effort: 'low' };

beforeEach(() => {
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) return { workers: [{ state: 'current', routes: [{ ...defaultRoute, schema_version: 2 }, { ...alternate, schema_version: 2, reason: 'unknown' }] }] } as T;
    return undefined as T;
  });
});
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

test('shows the project default and offers a configured local model with its effort without submitting', async () => {
  const onChange = vi.fn();
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await waitFor(() => expect(screen.getByRole('option', { name: /Project default · gpt-6-luna/ })).toBeInTheDocument());
  expect(screen.getByRole('combobox', { name: 'Effort' })).toHaveValue('medium');
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']));
  expect(onChange).toHaveBeenCalledWith(alternate);
});

test('stale or absent runtime metadata leaves the project default usable', async () => {
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.includes('subscription-profile')
    ? { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T
    : { workers: [] } as T);
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default'));
  expect(screen.getByRole('combobox', { name: 'Model' })).toBeEnabled();
});

test('stale runtime metadata does not advertise alternatives or disable the default', async () => {
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.includes('subscription-profile')
    ? { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T
    : { workers: [{ state: 'stale', routes: [alternate] }] } as T);
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toBeEnabled());
  expect(screen.getByRole('combobox', { name: 'Effort' })).toHaveValue('medium');
  expect(screen.queryByRole('option', { name: /gpt-6-astra/ })).not.toBeInTheDocument();
});

test('offers a maximum effort local variant with unknown admission evidence', async () => {
  const maximum = { ...defaultRoute, effort: 'maximum' };
  const onChange = vi.fn();
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.includes('subscription-profile')
    ? { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T
    : { workers: [{ state: 'current', routes: [defaultRoute, { ...maximum, effective_reason: 'operator_trusted', admitted: true, warnings: ['approved_tools_unproved'] }] }] } as T);
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await userEvent.selectOptions(await screen.findByRole('combobox', { name: 'Effort' }), 'maximum');
  expect(onChange).toHaveBeenCalledWith(maximum);
});

test('excludes configured current routes explicitly denied admission while retaining trusted routes', async () => {
  const denied = { ...alternate, model: 'missing-client', configured: true, admitted: false, effective_reason: 'missing_executable' };
  const trusted = { ...alternate, model: 'trusted-client', configured: true, admitted: true, effective_reason: 'operator_trusted', warnings: ['approved_tools_unproved'] };
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.includes('subscription-profile')
    ? { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T
    : { workers: [{ state: 'current', routes: [denied, trusted] }] } as T);
  const onChange = vi.fn();
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  expect(await screen.findByRole('option', { name: /trusted-client/ })).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: /missing-client/ })).not.toBeInTheDocument();
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), JSON.stringify(['openai', 'codex_app_server', 'trusted-client']));
  expect(onChange).toHaveBeenCalledWith({ ...alternate, model: 'trusted-client' });
});

test.each([
  ['stale worker', { state: 'stale', admitted: true }],
  ['stopped worker', { state: 'stopped', admitted: true }],
  ['empty inventory', { state: 'empty', admitted: true }],
  ['denied admission', { state: 'current', admitted: false }],
] as const)('clears an explicit choice when a refresh reports %s', async (_name, next) => {
  const onChange = vi.fn();
  let runtimeReads = 0;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      runtimeReads += 1;
      const observed = new Date().toISOString();
      return { observed_at: observed, fresh_for_seconds: 2, workers: next.state === 'empty' && runtimeReads > 1 ? [] : [{
        state: runtimeReads === 1 ? 'current' : next.state, last_seen_at: observed,
        routes: [{ ...alternate, configured: true, admitted: runtimeReads === 1 ? true : next.admitted }],
      }] } as T;
    }
    return undefined as T;
  });
  const view = render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await screen.findByRole('option', { name: /gpt-6-astra/ });
  view.rerender(<BrainstormModelPicker projectId="project" choice={alternate} onChange={onChange} />);
  expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue(JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']));
  await waitFor(() => expect(onChange).toHaveBeenCalledWith(null, 'unavailable'), { timeout: 3000 });
  expect(runtimeReads).toBeGreaterThan(1);
  expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default');
});

test.each(['pending', 'failed'] as const)('expires a retained route while refresh is %s and keeps the default usable', async refreshState => {
  const onChange = vi.fn();
  let runtimeReads = 0;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      runtimeReads += 1;
      if (runtimeReads > 1) {
        if (refreshState === 'failed') throw new Error('runtime offline');
        return await new Promise<T>(() => {});
      }
      const observed = new Date().toISOString();
      return { observed_at: observed, fresh_for_seconds: 1, workers: [
        { state: 'current', last_seen_at: observed, routes: [{ ...alternate, admitted: true, configured: true }] },
      ] } as T;
    }
    return undefined as T;
  });
  const view = render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await screen.findByRole('option', { name: /gpt-6-astra/ });
  view.rerender(<BrainstormModelPicker projectId="project" choice={alternate} onChange={onChange} />);
  await waitFor(() => expect(onChange).toHaveBeenCalledWith(null, 'unavailable'), { timeout: 3000 });
  expect(runtimeReads).toBeGreaterThan(1);
  expect(screen.getByRole('combobox', { name: 'Model' })).toBeEnabled();
  expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default');
});

test('clears a selected route on project change even if another project advertises the same route', async () => {
  const onChange = vi.fn();
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.includes('subscription-profile')
    ? { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T
    : { workers: [{ state: 'current', routes: [{ ...alternate, admitted: true, configured: true }] }] } as T);
  const view = render(<BrainstormModelPicker projectId="first-project" choice={null} onChange={onChange} />);
  await screen.findByRole('option', { name: /gpt-6-astra/ });
  view.rerender(<BrainstormModelPicker projectId="first-project" choice={alternate} onChange={onChange} />);
  expect(onChange).not.toHaveBeenCalled();
  view.rerender(<BrainstormModelPicker projectId="second-project" choice={alternate} onChange={onChange} />);
  expect(onChange).toHaveBeenCalledWith(null, 'unavailable');
  view.rerender(<BrainstormModelPicker projectId="second-project" choice={null} onChange={onChange} />);
  expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default');
});

test('clears a selected effort when only another effort of the same model remains', async () => {
  const onChange = vi.fn();
  const high: BrainstormRoute = { ...alternate, effort: 'high' };
  let reads = 0;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      reads += 1;
      const observed = new Date().toISOString();
      return { observed_at: observed, fresh_for_seconds: 2, workers: [{ state: 'current', last_seen_at: observed,
        routes: reads === 1 ? [alternate, high] : [alternate] }] } as T;
    }
    return undefined as T;
  });
  const view = render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await screen.findByRole('option', { name: /gpt-6-astra/ });
  view.rerender(<BrainstormModelPicker projectId="project" choice={alternate} onChange={onChange} />);
  await screen.findByRole('option', { name: 'high' });
  view.rerender(<BrainstormModelPicker projectId="project" choice={high} onChange={onChange} />);
  await waitFor(() => expect(onChange).toHaveBeenCalledWith(null, 'unavailable'), { timeout: 3000 });
  expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default');
});

test('keeps a shared route while one worker becomes stale and a current copy remains', async () => {
  const onChange = vi.fn();
  let reads = 0;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      reads += 1;
      const observed = new Date().toISOString();
      return { observed_at: observed, fresh_for_seconds: 2, workers: [
        { state: reads === 1 ? 'current' : 'stale', last_seen_at: observed, routes: [alternate] },
        { state: 'current', last_seen_at: observed, routes: [{ ...alternate, admitted: true }] },
      ] } as T;
    }
    return undefined as T;
  });
  const view = render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await screen.findByRole('option', { name: /gpt-6-astra/ });
  view.rerender(<BrainstormModelPicker projectId="project" choice={alternate} onChange={onChange} />);
  await waitFor(() => expect(reads).toBeGreaterThan(1), { timeout: 3000 });
  expect(onChange).not.toHaveBeenCalled();
  expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue(JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']));
});

test('repeated copies of one observation cannot renew an aging route', async () => {
  const onChange = vi.fn();
  const observed = new Date().toISOString();
  let reads = 0;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      reads += 1;
      if (reads > 2) return await new Promise<T>(() => {});
      return { observed_at: observed, fresh_for_seconds: 2, workers: [
        { state: 'current', last_seen_at: observed, routes: [alternate] },
      ] } as T;
    }
    return undefined as T;
  });
  const view = render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await screen.findByRole('option', { name: /gpt-6-astra/ });
  view.rerender(<BrainstormModelPicker projectId="project" choice={alternate} onChange={onChange} />);
  await waitFor(() => expect(reads).toBeGreaterThan(1), { timeout: 2500 });
  expect(onChange).not.toHaveBeenCalled();
  await waitFor(() => expect(onChange).toHaveBeenCalledWith(null, 'unavailable'), { timeout: 2500 });
});

test('stops polling runtime after the picker unmounts', async () => {
  let reads = 0;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      reads += 1;
      const observed = new Date().toISOString();
      return { observed_at: observed, fresh_for_seconds: 1, workers: [
        { state: 'current', last_seen_at: observed, routes: [alternate] },
      ] } as T;
    }
    return undefined as T;
  });
  const view = render(<BrainstormModelPicker projectId="project" choice={null} onChange={vi.fn()} />);
  await screen.findByRole('option', { name: /gpt-6-astra/ });
  view.unmount();
  await new Promise(resolve => setTimeout(resolve, 1200));
  expect(reads).toBe(1);
});

test('settled denied refresh clears the next send before deferred snapshot bookkeeping', async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-09T12:00:00Z'));
  const sent: Array<BrainstormRoute | null> = [];
  let runtimeReads = 0;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      runtimeReads += 1;
      const observed = new Date().toISOString();
      return { observed_at: observed, fresh_for_seconds: 2, workers: [{ state: 'current', last_seen_at: observed,
        routes: [{ ...alternate, admitted: runtimeReads === 1, configured: true }] }] } as T;
    }
    return undefined as T;
  });
  function Harness() {
    const [choice, setChoice] = useState<BrainstormRoute | null>(null);
    return <><BrainstormModelPicker projectId="project" choice={choice} onChange={setChoice} />
      <button onClick={() => sent.push(choice)}>Send next message</button></>;
  }
  const view = render(<Harness />);
  try {
    await act(async () => { await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(0); await Promise.resolve(); });
    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), { target: { value: JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']) } });
    expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue(JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']));
    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); });
    expect(runtimeReads).toBe(2);
    fireEvent.click(screen.getByRole('button', { name: 'Send next message' }));
    expect(sent).toEqual([null]);
  } finally {
    view.unmount();
    vi.useRealTimers();
  }
});

test('a repeated expired observation does not revive choices before deferred bookkeeping', async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-09T12:00:00Z'));
  const observed = new Date().toISOString();
  let resolveRefresh!: () => void;
  let runtimeReads = 0;
  const page = { observed_at: observed, fresh_for_seconds: 2, workers: [
    { state: 'current', last_seen_at: observed, routes: [{ ...alternate, admitted: true, configured: true }] },
  ] };
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      runtimeReads += 1;
      return runtimeReads === 1 ? page as T : await new Promise<T>(resolve => { resolveRefresh = () => resolve(page as T); });
    }
    return undefined as T;
  });
  const view = render(<BrainstormModelPicker projectId="project" choice={null} onChange={vi.fn()} />);
  try {
    await act(async () => { await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(0); await Promise.resolve(); });
    expect(screen.getByRole('option', { name: /gpt-6-astra/ })).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(2001); await Promise.resolve(); });
    expect(runtimeReads).toBe(2);
    expect(screen.queryByRole('option', { name: /gpt-6-astra/ })).not.toBeInTheDocument();
    await act(async () => { resolveRefresh(); await Promise.resolve(); });
    expect(screen.queryByRole('option', { name: /gpt-6-astra/ })).not.toBeInTheDocument();
  } finally {
    view.unmount();
    vi.useRealTimers();
  }
});

test.each(['empty', 'failed'] as const)('offers only the project default when runtime is %s', async runtimeState => {
  const onChange = vi.fn();
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.includes('subscription-profile')) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
    if (path.includes('subscription-runtime')) {
      if (runtimeState === 'failed') throw new Error('runtime unavailable');
      return { workers: [] } as T;
    }
    return undefined as T;
  });
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toBeEnabled());
  expect(screen.getAllByRole('option', { name: /Project default/ })).toHaveLength(1);
  expect(screen.getByRole('combobox', { name: 'Model' }).querySelectorAll('option')).toHaveLength(1);
  fireEvent.change(screen.getByRole('combobox', { name: 'Effort' }), { target: { value: 'medium' } });
  expect(onChange).not.toHaveBeenCalledWith(defaultRoute);
});

test('returns from an explicit default-model effort to the project default when that effort is not configured', async () => {
  const high: BrainstormRoute = { ...defaultRoute, effort: 'high' };
  const onChange = vi.fn();
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.includes('subscription-profile')
    ? { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T
    : { workers: [{ state: 'current', routes: [high] }] } as T);
  const view = render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await screen.findByRole('option', { name: 'high' });
  fireEvent.change(screen.getByRole('combobox', { name: 'Effort' }), { target: { value: 'high' } });
  expect(onChange).toHaveBeenCalledWith(high);
  view.rerender(<BrainstormModelPicker projectId="project" choice={high} onChange={onChange} />);
  fireEvent.change(screen.getByRole('combobox', { name: 'Effort' }), { target: { value: 'medium' } });
  expect(onChange).toHaveBeenLastCalledWith(null);
});

test('changing effort on the default model creates an explicit configured choice', async () => {
  const onChange = vi.fn();
  const higherEffort = { ...defaultRoute, effort: 'high' };
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.includes('subscription-profile')
    ? { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T
    : { workers: [{ state: 'current', routes: [defaultRoute, higherEffort] }] } as T);
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await waitFor(() => expect(screen.getByRole('option', { name: 'high' })).toBeInTheDocument());
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Effort' }), 'high');
  expect(onChange).toHaveBeenCalledWith(higherEffort);
});

test('offers maximum default and current configured routes while excluding stale alternatives', async () => {
  const maximumDefault = { ...defaultRoute, effort: 'maximum' };
  const staleOnly = { ...alternate, model: 'stale-only' };
  const stoppedOnly = { ...alternate, model: 'stopped-only' };
  const liveDuplicate = { ...alternate, model: 'live-duplicate' };
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.includes('subscription-profile')
    ? { preferences: [{ purpose: 'exploration', preferred_route: maximumDefault }] } as T
    : { workers: [
      { state: 'stale', routes: [staleOnly, liveDuplicate] },
      { state: 'stopped', routes: [stoppedOnly] },
      { state: 'current', routes: [
        { ...alternate, model: 'stale-reason', effective_reason: 'stale_worker' },
        { ...liveDuplicate, effective_reason: 'operator_trusted' },
      ] },
    ] } as T);
  const onChange = vi.fn();
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  expect(await screen.findByRole('option', { name: 'maximum' })).toBeInTheDocument();
  expect(screen.getByRole('combobox', { name: 'Model' })).toBeEnabled();
  expect(screen.getByRole('combobox', { name: 'Effort' })).toHaveValue('maximum');
  expect(screen.queryByRole('option', { name: /stale-only/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('option', { name: /stopped-only/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('option', { name: /stale-reason/ })).not.toBeInTheDocument();
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), JSON.stringify(['openai', 'codex_app_server', 'live-duplicate']));
  expect(onChange).toHaveBeenCalledWith(liveDuplicate);
});
