import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProfileSelector } from './profile-selector';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const profile = (version: number) => ({ profile_id: 'profile-1', version, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [] });
test('selects a profile with paired expected identity and stable retry key', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch')
    .mockRejectedValueOnce(new TypeError('offline'))
    .mockResolvedValue(new Response('{}', { status: 201 }));
  const refresh = vi.fn();
  render(<ProfileSelector projectId="project-1" profiles={[profile(1), profile(2)]} current={profile(1)} refresh={refresh} />);
  await userEvent.selectOptions(screen.getByLabelText('Profile version'), 'profile-1:2');
  await userEvent.click(screen.getByRole('button', { name: 'Select profile version' }));
  const request = fetcher.mock.calls[0][1] as RequestInit;
  expect(JSON.parse(String(request.body))).toEqual({ profile_id: 'profile-1', profile_version: 2, expected_profile_id: 'profile-1', expected_profile_version: 1 });
  const retryKey = new Headers(request.headers).get('Idempotency-Key');
  expect(retryKey).toBeTruthy();
  await screen.findByRole('alert');
  await userEvent.click(screen.getByRole('button', { name: 'Select profile version' }));
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(new Headers(fetcher.mock.calls[1][1]?.headers).get('Idempotency-Key')).toBe(retryKey);
  expect(refresh).toHaveBeenCalledOnce();
});

test('displays saved Jev default in selection options and current selection details', () => {
  const profileWithJev = {
    profile_id: 'profile-1',
    version: 2,
    default_billing_mode: 'allowance_only' as const,
    approved_mappings: [],
    preferences: [],
    jev: {
      mode: 'shadow' as const,
      allow_remote: true,
      model: 'jev-fast',
      semantic_search: true,
      review_focus: true,
      top_k: 10,
      max_requests_per_run: 50,
      max_input_units_per_run: 100000,
      max_candidates: 50,
      max_result_chars: 10000,
      timeout_seconds: 10,
      cache_ttl_seconds: 1800,
    },
  };
  render(
    <ProfileSelector
      projectId="project-1"
      profiles={[profile(1), profileWithJev]}
      current={profileWithJev}
      refresh={vi.fn()}
    />
  );
  expect(screen.getByRole('option', { name: /Version 2 · profile-1 \(Jev: Shadow\)/ })).toBeInTheDocument();
  expect(screen.getByText(/Jev default:/)).toBeInTheDocument();
  expect(screen.getByText('Shadow (jev-fast)')).toBeInTheDocument();
});

test('surfaces primary and preferred role models with reasoning and link to edit profiles', () => {
  const profileWithRoles = {
    profile_id: 'profile-roles',
    version: 3,
    default_billing_mode: 'allowance_only' as const,
    approved_mappings: [],
    preferences: [
      {
        purpose: 'primary' as const,
        preferred_route: {
          provider: 'google',
          client: 'gemini_cli',
          model: 'gemini-3.8-flash',
          effort: 'medium' as const,
          auth_mode: 'subscription' as const,
          billing_mode: 'allowance_only' as const,
        },
        fallback_routes: [],
      },
      {
        purpose: 'independent_review' as const,
        preferred_route: {
          provider: 'anthropic',
          client: 'claude_code',
          model: 'claude-opus-5-5',
          effort: 'high' as const,
          auth_mode: 'subscription' as const,
          billing_mode: 'allowance_only' as const,
        },
        fallback_routes: [],
      },
    ],
  };

  render(
    <ProfileSelector
      projectId="project-1"
      profiles={[profileWithRoles]}
      current={profileWithRoles}
      refresh={vi.fn()}
    />
  );

  expect(screen.getByText(/gemini-3.8-flash/)).toBeInTheDocument();
  expect(screen.getByText(/medium/i)).toBeInTheDocument();
  expect(screen.getByText(/claude-opus-5-5/)).toBeInTheDocument();
  const editLink = screen.getByRole('link', { name: /Edit subscription profiles/i });
  expect(editLink).toHaveAttribute('href', '/subscription-profiles');
});
