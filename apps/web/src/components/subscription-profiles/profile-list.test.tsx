import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProfileList, keyFor } from './profile-list';

const mockedClient = vi.hoisted(() => {
  class MockApiError extends Error { constructor(public status: number, code: string) { super(code); } }
  return { MockApiError, api: vi.fn() };
});
const defaultApi = async (path: string) => {
  if (path === '/subscription-models') {
    return { observed_at: '2026-09-29T20:00:00Z', catalogs: [] };
  }
  throw new mockedClient.MockApiError(409, 'stale-projection');
};
mockedClient.api.mockImplementation(defaultApi);
vi.mock('@/lib/api/client', () => ({ ApiError: mockedClient.MockApiError, api: mockedClient.api }));

afterEach(() => {
  cleanup();
  mockedClient.api.mockReset();
  mockedClient.api.mockImplementation(defaultApi);
});
const profile = (version: number) => ({ profile_id: 'profile-1', version, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [{ purpose: 'primary', preferred_route: { provider: 'openai', client: 'codex', model: `model-${version}`, effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' }, fallback_routes: [] }] });
test('keeps saved role details secondary and expands each role independently', async () => {
  const saved = profile(1);
  saved.preferences.push({
    ...saved.preferences[0],
    purpose: 'planning',
    preferred_route: { ...saved.preferences[0].preferred_route, provider: 'google', client: 'gemini_cli', model: 'planner-model' },
  });
  render(<ProfileList profiles={[saved]} refresh={vi.fn()} />);
  const latest = within(screen.getByRole('region', { name: 'Latest subscription profiles' }));
  const primaryClient = latest.getByText('Codex', { exact: true });
  const plannerClient = latest.getByText('Gemini CLI', { exact: true });

  expect(primaryClient).not.toBeVisible();
  await userEvent.click(latest.getByText('Assigned roles', { exact: true }));
  expect(latest.getByText('Primary', { exact: true })).toBeVisible();
  expect(latest.getByText('Planner', { exact: true })).toBeVisible();
  expect(primaryClient).not.toBeVisible();
  expect(plannerClient).not.toBeVisible();

  await userEvent.click(latest.getByText('Primary', { exact: true }));
  expect(primaryClient).toBeVisible();
  expect(plannerClient).not.toBeVisible();
  await userEvent.click(latest.getByText('Primary', { exact: true }));
  expect(primaryClient).not.toBeVisible();
  expect(primaryClient).toBeInTheDocument();
});

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
  expect(keyFor({ path: '/subscription-profiles', body: { max_input_tokens: '9007199254740992' } })).not.toBe(
    keyFor({ path: '/subscription-profiles', body: { max_input_tokens: '9007199254740993' } })
  );
});

test.each(['create', 'append'])('sends exact numeric profile caps and replays the same body/key after uncertain %s', async mode => {
  const requests: Array<{ path: string; body: string; key: string }> = [];
  mockedClient.api.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === '/subscription-models') return { catalogs: [] };
    requests.push({ path, body: String(options?.body),
      key: String((options?.headers as Record<string, string>)['Idempotency-Key']) });
    if (requests.length === 1) throw new Error('response lost');
    return profile(mode === 'create' ? 1 : 2);
  });
  const refresh = vi.fn();
  render(<ProfileList profiles={mode === 'create' ? [] : [profile(1)]} refresh={refresh} />);
  if (mode === 'append') {
    await userEvent.click(screen.getByText(/Saved profile history/));
    await userEvent.click(screen.getByRole('button', { name: 'Append from latest version 1' }));
  }
  const form = screen.getByRole('form', { name: mode === 'create' ? 'Create subscription profile' : 'Append profile version 1' });
  const fields = within(form);
  await userEvent.click(fields.getAllByText('Advanced')[0]);
  fireEvent.change(fields.getByRole('textbox', { name: 'Primary input token budget' }),
    { target: { value: '9007199254740993' } });
  fireEvent.change(fields.getByRole('textbox', { name: 'Primary output token budget' }),
    { target: { value: '9223372036854775807' } });
  const save = fields.getByRole('button', { name: mode === 'create' ? 'Create profile version 1' : 'Append version 2' });
  await userEvent.click(save);
  expect(await fields.findByRole('alert')).toHaveTextContent('response lost');
  expect(requests).toHaveLength(1);
  expect(requests[0].body).toMatch(/"max_input_tokens":9007199254740993\b/);
  expect(requests[0].body).toMatch(/"max_output_tokens":9223372036854775807\b/);
  expect(requests[0].body).toMatch(/"purpose":"primary"/);
  expect(requests[0].path).toBe(mode === 'create' ? '/subscription-profiles' : '/subscription-profiles/profile-1/versions');
  if (mode === 'append') expect(requests[0].body).toMatch(/"expected_current_version":1\b/);
  await userEvent.click(save);
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
  expect(refresh).toHaveBeenCalledOnce();
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
  const input = fields.getByRole('textbox', { name: 'Primary input token budget' });
  await userEvent.type(input, '262500');
  const output = fields.getByRole('textbox', { name: 'Primary output token budget' });
  await userEvent.click(fields.getByRole('button', { name: mode === 'create' ? 'Create profile version 1' : 'Append version 2' }));
  expect(input).toBeDisabled();
  expect(model).toBeDisabled();
  expect(fields.getByLabelText('Primary reasoning')).toBeDisabled();
  expect(output).toBeDisabled();
  for (const operation of fields.getAllByRole('button', { name: mode === 'create' ? 'Remove role' : 'Add role preference' })) {
    expect(operation).toBeDisabled();
  }
  await userEvent.type(input, '9');
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
    expect(input).toHaveValue('262500');
    await userEvent.clear(input);
    await userEvent.type(input, '525000');
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

  expect(screen.getByText('On (jev-latest)').closest('p'))
    .toHaveTextContent('Jev default: On (jev-latest) · remote processing allowed');
  const latest = within(screen.getByRole('region', { name: 'Latest subscription profiles' }));
  expect(latest.getByText('None', { exact: true }).closest('p')).toHaveTextContent('Jev default: None');
});

test('latest profiles are discoverable with derived human routing labels while history remains secondary', () => {
  const p1 = profile(1);
  const p2 = profile(2);
  const { container } = render(<ProfileList profiles={[p1, p2]} refresh={vi.fn()} />);

  // Latest profile card is discoverable without opening any closed ancestor
  const latestArticle = screen.getAllByRole('article')[0];
  expect(latestArticle).toBeInTheDocument();
  const parentDetails = latestArticle.closest('details');
  // Latest article is NOT inside a closed details element
  if (parentDetails) {
    expect(parentDetails).toHaveAttribute('open');
  }

  // Derived human summary label is present in heading
  expect(screen.getByRole('heading', { level: 3, name: /model-2/i })).toBeInTheDocument();

  // #profile-editor element exists and is mounted
  expect(container.querySelector('#profile-editor')).toBeInTheDocument();
});
