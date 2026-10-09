import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { api } from '@/lib/api/client';
import { BrainstormModelPicker } from './brainstorm-model-picker';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

const defaultRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-luna', effort: 'medium', auth_mode: 'subscription', billing_mode: 'allowance_only' };
const alternate = { ...defaultRoute, model: 'gpt-6-astra', effort: 'low' };

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
    : { workers: [{ state: 'current', routes: [defaultRoute, { ...maximum, effective_reason: 'unknown', admitted: false }] }] } as T);
  render(<BrainstormModelPicker projectId="project" choice={null} onChange={onChange} />);
  await userEvent.selectOptions(await screen.findByRole('combobox', { name: 'Effort' }), 'maximum');
  expect(onChange).toHaveBeenCalledWith(maximum);
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
