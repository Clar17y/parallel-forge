import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProfileSelector } from './profile-selector';

afterEach(cleanup);
const profile = (version: number) => ({ profile_id: 'profile-1', version, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [] });
test('selects a profile with paired expected identity and stable retry key', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 201 }));
  const refresh = vi.fn();
  render(<ProfileSelector projectId="project-1" profiles={[profile(1), profile(2)]} current={profile(1)} refresh={refresh} />);
  await userEvent.selectOptions(screen.getByLabelText('Profile version'), 'profile-1:2');
  await userEvent.click(screen.getByRole('button', { name: 'Select profile version' }));
  const request = fetcher.mock.calls[0][1] as RequestInit;
  expect(JSON.parse(String(request.body))).toEqual({ profile_id: 'profile-1', profile_version: 2, expected_profile_id: 'profile-1', expected_profile_version: 1 });
  expect(new Headers(request.headers).get('Idempotency-Key')).toBeTruthy();
  fetcher.mockRestore();
});
