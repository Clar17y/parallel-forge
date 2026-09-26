import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProfileList, keyFor } from './profile-list';

const mockedClient = vi.hoisted(() => {
  class MockApiError extends Error { constructor(public status: number, code: string) { super(code); } }
  return { MockApiError, api: vi.fn() };
});
mockedClient.api.mockRejectedValue(new mockedClient.MockApiError(409, 'stale-projection'));
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
  await userEvent.click(screen.getByRole('button', { name: 'Reload profile history' }));
  expect(refresh).toHaveBeenCalled();
});
