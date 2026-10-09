import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
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
test('keeps historical versions visible and labels them immutable', async () => {
  render(<ProfileList profiles={[profile(1), profile(2)]} refresh={vi.fn()} />);
  await userEvent.click(screen.getByText(/Saved profile history/));
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
  await userEvent.click(screen.getByText(/Saved profile history/));
  await userEvent.click(screen.getByRole('button', { name: 'Append from latest version 1' }));
  await userEvent.click(screen.getByText('Advanced'));
  await userEvent.type(screen.getByLabelText('Primary provider'), 'ignored');
  await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
  expect(await screen.findByText(/changed in another tab/)).toBeInTheDocument();
  expect(screen.getByLabelText('Primary provider')).toHaveValue('openaiignored');
  expect(screen.getByRole('button', { name: 'Append version 2' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Reload profile history' }));
  expect(refresh).toHaveBeenCalled();
});

test('distinguishes latest and historical profile versions with readable badges', async () => {
  render(<ProfileList profiles={[profile(1), profile(2)]} refresh={vi.fn()} />);
  await userEvent.click(screen.getByText(/Saved profile history/));
  expect(screen.getByText('Latest version')).toBeInTheDocument();
  expect(screen.getByText('Historical version')).toBeInTheDocument();
  expect(within(screen.getAllByRole('article')[0]).getAllByText('OpenAI')).toHaveLength(1);
  expect(within(screen.getAllByRole('article')[1]).getAllByText('OpenAI')).toHaveLength(1);
});

test('puts the current profile first and keeps historical role details expandable', async () => {
  const versions = [profile(1), profile(2)];
  render(<ProfileList profiles={versions} refresh={vi.fn()} />);
  await userEvent.click(screen.getByText(/Saved profile history/));
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
  await userEvent.click(screen.getByText(/Saved profile history/));
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

test.each([
  ['create', 'success'], ['create', 'failure'], ['append', 'success'], ['append', 'failure'],
])('protects budget controls through a pending %s save and %s settlement', async (mode, outcome) => {
  let resolve!: (value: unknown) => void;
  let reject!: (error: Error) => void;
  const posted: Record<string, unknown>[] = [];
  mockedClient.api.mockImplementation((path: string, options?: RequestInit) => {
    if (path === '/subscription-models') return Promise.resolve({ catalogs: [] });
    posted.push(JSON.parse(String(options?.body)));
    return new Promise((done, fail) => { resolve = done; reject = fail; });
  });
  const refresh = vi.fn();
  render(<ProfileList profiles={mode === 'create' ? [] : [profile(1)]} refresh={refresh} />);
  if (mode === 'append') {
    await userEvent.click(screen.getByText(/Saved profile history/));
    await userEvent.click(screen.getByRole('button', { name: 'Append from latest version 1' }));
  }
  const form = screen.getByRole('form', { name: mode === 'create' ? 'Create subscription profile' : 'Append profile version 1' });
  const fields = within(form);
  const model = fields.getByLabelText('Primary model') as HTMLSelectElement;
  await userEvent.selectOptions(model, Array.from(model.options).find(option => option.textContent === 'Sol 6.1')!.value);
  await userEvent.click(fields.getAllByText('Advanced')[0]);
  await userEvent.click(fields.getByRole('button', { name: 'Set Primary input token budget' }));
  const input = fields.getByRole('slider', { name: 'Primary input token budget' });
  const inputControls = within(input.parentElement!);
  await userEvent.click(inputControls.getByRole('button', { name: '25%' }));
  await userEvent.click(inputControls.getByLabelText('Exact value'));
  const exact = fields.getByRole('spinbutton', { name: 'Primary input token budget exact value' });
  const enableOutput = fields.getByRole('button', { name: 'Set Primary output token budget' });
  await userEvent.click(fields.getByRole('button', { name: mode === 'create' ? 'Create profile version 1' : 'Append version 2' }));
  expect(input).toBeDisabled();
  expect(model).toBeDisabled();
  expect(fields.getByLabelText('Primary reasoning')).toBeDisabled();
  expect(exact).toBeDisabled();
  expect(inputControls.getByLabelText('Exact value')).toBeDisabled();
  expect(inputControls.getByRole('button', { name: '50%' })).toBeDisabled();
  expect(enableOutput).toBeDisabled();
  expect(fields.getByRole('button', { name: 'Primary input tokens use run/task default' })).toBeDisabled();
  for (const operation of fields.getAllByRole('button', { name: mode === 'create' ? 'Remove role' : 'Add role preference' })) {
    expect(operation).toBeDisabled();
  }
  await userEvent.click(inputControls.getByRole('button', { name: '50%' }));
  fireEvent.submit(form);
  expect(posted).toHaveLength(1);
  expect(input).toHaveValue('262500');
  expect(posted[0].preferences).toEqual(expect.arrayContaining([expect.objectContaining({ purpose: 'primary', token_budget: { max_input_tokens: 262500 } })]));
  await act(async () => outcome === 'success' ? resolve(profile(mode === 'create' ? 1 : 2)) : reject(new Error('Save failed')));
  if (outcome === 'success') {
    expect(refresh).toHaveBeenCalledOnce();
    expect(form).not.toBeInTheDocument();
    expect(screen.getByRole('form', { name: 'Create subscription profile' })).toBeInTheDocument();
  } else {
    expect(fields.getByRole('alert')).toHaveTextContent('Save failed');
    expect(input).toBeEnabled();
    expect(exact).toHaveValue(262500);
    await userEvent.click(inputControls.getByRole('button', { name: '50%' }));
    expect(input).toHaveValue('525000');
    expect(posted).toHaveLength(1);
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
  const select = screen.getByLabelText('Primary model') as HTMLSelectElement;
  const alternate = Array.from(select.options).find(option => option.textContent?.includes('Sol 6.1'))!;
  await userEvent.selectOptions(select, alternate.value);
  const draftValue = select.value;
  const reasoning = screen.getByLabelText('Primary reasoning') as HTMLSelectElement;
  expect(reasoning).toHaveValue('maximum');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('temporary failure');
  expect(screen.getByLabelText('Primary model')).toHaveValue(draftValue);
  expect(screen.getByLabelText('Primary reasoning')).toHaveValue('maximum');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(refresh).toHaveBeenCalledOnce();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect(screen.getByLabelText('Primary model')).not.toBe(select);
  expect(screen.getByLabelText('Primary model')).not.toHaveValue(draftValue);
  expect(screen.getByLabelText('Primary reasoning')).toHaveValue('low');
});

test('displays saved Jev default in profile version card details', async () => {
  const profileWithJev = {
    ...profile(1),
    jev: {
      mode: 'on' as const,
      allow_remote: true,
      model: 'jev-latest',
      semantic_search: true,
      review_focus: true,
      top_k: 15,
      max_requests_per_run: 64,
      max_input_units_per_run: 250000,
      max_candidates: 96,
      max_result_chars: 12000,
      timeout_seconds: 15,
      cache_ttl_seconds: 3600,
    },
  };
  const profileWithoutJev = profile(2);

  render(<ProfileList profiles={[profileWithJev, profileWithoutJev]} refresh={vi.fn()} />);
  await userEvent.click(screen.getByText(/Saved profile history/));

  expect(screen.getByText('On (jev-latest)')).toBeInTheDocument();
  expect(screen.getByText('Jev default: On · jev-latest · remote processing allowed')).toBeInTheDocument();
  expect(screen.getByText('Jev default: None')).toBeInTheDocument();
});
