import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProfileList, keyFor } from './profile-list';

const mockedClient = vi.hoisted(() => {
  class MockApiError extends Error { constructor(public status: number, code: string) { super(code); } }
  return { MockApiError, api: vi.fn() };
});
mockedClient.api.mockImplementation(async (path: string) => {
  if (path === '/subscription-models') {
    return { observed_at: '2026-09-29T20:00:00Z', catalogs: [] };
  }
  throw new mockedClient.MockApiError(409, 'stale-projection');
});
vi.mock('@/lib/api/client', () => ({ ApiError: mockedClient.MockApiError, api: mockedClient.api }));

afterEach(cleanup);
const profile = (version: number) => ({ profile_id: 'profile-1', version, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [{ purpose: 'primary', preferred_route: { provider: 'openai', client: 'codex', model: `model-${version}`, effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' }, fallback_routes: [] }] });
test('keeps historical versions visible and labels them immutable', () => {
  render(<ProfileList profiles={[profile(1), profile(2)]} refresh={vi.fn()} />);
  expect(screen.getByRole('heading', { name: /version 1/ })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: /version 2/ })).toBeInTheDocument();
  expect(screen.getAllByText(/immutable configuration/)).toHaveLength(2);
});

test('derives the same retry key only for the same request payload', () => {
  expect(keyFor({ path: '/subscription-profiles', body: { x: 1 } })).toBe(keyFor({ path: '/subscription-profiles', body: { x: 1 } }));
  expect(keyFor({ path: '/subscription-profiles', body: { x: 1 } })).not.toBe(keyFor({ path: '/subscription-profiles', body: { x: 2 } }));
});

test('offers an explicit reload after a stale append response', async () => {
  const refresh = vi.fn();
  render(<ProfileList profiles={[profile(1)]} refresh={refresh} />);
  await userEvent.click(screen.getByRole('button', { name: 'Append from latest version 1' }));
  await userEvent.type(screen.getByLabelText('Preferred route provider'), 'ignored');
  await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
  expect(await screen.findByText(/changed in another tab/)).toBeInTheDocument();
  expect(screen.getByLabelText('Preferred route provider')).toHaveValue('openaiignored');
  expect(screen.getByRole('button', { name: 'Append version 2' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Reload profile history' }));
  expect(refresh).toHaveBeenCalled();
});

test('distinguishes latest and historical profile versions with readable badges', () => {
  render(<ProfileList profiles={[profile(1), profile(2)]} refresh={vi.fn()} />);
  expect(screen.getByText('Latest version')).toBeInTheDocument();
  expect(screen.getByText('Historical version')).toBeInTheDocument();
  expect(screen.getAllByText('OpenAI')).toHaveLength(2);
});

test('puts the current profile first and keeps historical role details expandable', async () => {
  const versions = [profile(1), profile(2)];
  render(<ProfileList profiles={versions} refresh={vi.fn()} />);
  expect(screen.getAllByRole('article')[0]).toHaveTextContent('version 2');
  const historical = screen.getByText('Show historical roles').closest('details');
  expect(historical).not.toHaveAttribute('open');
  await userEvent.click(screen.getByText('Show historical roles'));
  expect(historical).toHaveAttribute('open');
  expect(versions.map(item => item.version)).toEqual([1, 2]);
});

test.each(['success', 'failure'])('keeps the editor attached while a profile append is pending (%s)', async outcome => {
  let resolve!: (value: unknown) => void;
  let reject!: (error: Error) => void;
  mockedClient.api.mockImplementation((path: string) => {
    if (path === '/subscription-models') {
      return Promise.resolve({ observed_at: '2026-09-29T20:00:00Z', catalogs: [] });
    }
    return new Promise((done, fail) => { resolve = done; reject = fail; });
  });
  const refresh = vi.fn();
  render(<ProfileList profiles={[profile(1)]} refresh={refresh} />);
  await userEvent.click(screen.getByRole('button', { name: 'Append from latest version 1' }));
  await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
  expect(screen.getByRole('button', { name: 'Cancel editing' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Append from latest version 1' })).toBeDisabled();
  await act(async () => outcome === 'success' ? resolve(profile(2)) : reject(new Error('Save failed')));
  if (outcome === 'success') {
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: 'Cancel editing' })).not.toBeInTheDocument();
  } else {
    expect(screen.getByRole('alert')).toHaveTextContent('Save failed');
    expect(screen.getByRole('button', { name: 'Cancel editing' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel editing' }));
    expect(screen.getByRole('form', { name: 'Create subscription profile' })).toBeInTheDocument();
  }
});

test('a confirmed create clears its draft even while history refresh is pending; a failed create keeps a retry key', async () => {
  let saves = 0;
  const keys: string[] = [];
  mockedClient.api.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === '/subscription-models') return { observed_at: '2026-09-29T20:00:00Z', catalogs: [] };
    keys.push(String((options?.headers as Record<string, string>)['Idempotency-Key']));
    if (++saves === 1) throw new Error('temporary failure');
    return profile(1);
  });
  const refresh = vi.fn();
  render(<ProfileList profiles={[]} refresh={refresh} />);
  await userEvent.type(screen.getByLabelText('Preferred route provider'), 'openai');
  await userEvent.type(screen.getByLabelText('Preferred route client'), 'codex_app_server');
  await userEvent.type(screen.getByLabelText('Preferred route model'), 'gpt-6-astra');
  const draft = screen.getByLabelText('Preferred route model');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('temporary failure');
  expect(draft).toHaveValue('gpt-6-astra');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(refresh).toHaveBeenCalledOnce();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect(screen.getByLabelText('Preferred route model')).not.toBe(draft);
  expect(screen.getByLabelText('Preferred route model')).toHaveValue('');
});
