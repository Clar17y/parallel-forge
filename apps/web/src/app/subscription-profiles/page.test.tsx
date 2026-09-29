import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, test, vi } from 'vitest';
import SubscriptionProfilesPage from './page';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const sampleProfile = {
  profile_id: 'test-profile-1',
  version: 1,
  default_billing_mode: 'allowance_only' as const,
  approved_mappings: [],
  preferences: [
    {
      purpose: 'primary' as const,
      preferred_route: {
        provider: 'openai',
        client: 'codex_app_server',
        model: 'gpt-6-astra',
        effort: 'low' as const,
        auth_mode: 'subscription' as const,
        billing_mode: 'allowance_only' as const,
      },
      fallback_routes: [],
    },
  ],
};

const mockFetch = (profilesData: unknown = [sampleProfile]) => {
  return vi.fn().mockImplementation((url: string) => {
    if (url.includes('/subscription-profiles')) {
      return Promise.resolve(new Response(JSON.stringify(profilesData)));
    }
    if (url.includes('/subscription-runtime')) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            observed_at: '2026-09-29T18:00:00Z',
            fresh_for_seconds: 45,
            workers: [],
            has_more: false,
          })
        )
      );
    }
    if (url.includes('/subscription-models')) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            observed_at: '2026-09-29T18:00:00Z',
            catalogs: [],
          })
        )
      );
    }
    return Promise.resolve(new Response(JSON.stringify({})));
  });
};

describe('SubscriptionProfilesPage', () => {
  test('renders page header, runtime status, and profile list without flicker', async () => {
    vi.stubGlobal('fetch', mockFetch());

    render(<SubscriptionProfilesPage />);

    expect(screen.getByRole('heading', { name: 'Subscription profiles' })).toBeInTheDocument();
    expect(screen.getByText(/Manage versioned requested routes and explicit fallbacks/i)).toBeInTheDocument();

    // Profiles load and display
    expect(await screen.findByRole('heading', { name: /Profile test-profile-1/i })).toBeInTheDocument();
  });

  test('keeps profile editor mounted and visible during background profile refresh', async () => {
    let failRefresh!: (error: Error) => void;
    let callCount = 0;

    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/subscription-profiles')) {
          callCount++;
          if (callCount === 1) {
            return Promise.resolve(new Response(JSON.stringify([sampleProfile])));
          }
          if (callCount === 2) return new Promise((_done, fail) => { failRefresh = fail; });
          return Promise.resolve(new Response(JSON.stringify([sampleProfile])));
        }
        if (url.includes('/subscription-runtime')) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                observed_at: '2026-09-29T18:00:00Z',
                fresh_for_seconds: 45,
                workers: [],
                has_more: false,
              })
            )
          );
        }
        if (url.includes('/subscription-models')) {
          return Promise.resolve(
            new Response(JSON.stringify({ observed_at: '2026-09-29T18:00:00Z', catalogs: [] }))
          );
        }
        return Promise.resolve(new Response(JSON.stringify({})));
      })
    );

    render(<SubscriptionProfilesPage />);

    expect(await screen.findByRole('heading', { name: /Profile test-profile-1/i })).toBeInTheDocument();

    // Open append editor
    await userEvent.click(screen.getByRole('button', { name: 'Append from latest version 1' }));
    expect(screen.getByRole('heading', { name: 'Append profile version 2' })).toBeInTheDocument();
    const model = screen.getByLabelText('Preferred route model');
    await userEvent.clear(model);
    await userEvent.type(model, 'draft-model');
    model.focus();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh profile history' }));
    model.focus();
    expect(screen.getByLabelText('Preferred route model')).toBe(model);
    expect(model).toHaveFocus();
    await act(async () => { failRefresh(new Error('offline')); });
    expect(screen.getByRole('alert')).toHaveTextContent('Showing the last loaded history');
    expect(screen.getByLabelText('Preferred route model')).toBe(model);
    expect(model).toHaveValue('draft-model');
    expect(screen.getByRole('button', { name: 'Append version 2' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByText(/Showing the last loaded history/)).not.toBeInTheDocument());
    expect(screen.getByLabelText('Preferred route model')).toBe(model);
    expect(model).toHaveValue('draft-model');
    expect(screen.getByRole('button', { name: 'Append version 2' })).toBeEnabled();
  });

  test('shows profile history error state when initial load fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/subscription-profiles')) {
          return Promise.reject(new Error('Network error'));
        }
        if (url.includes('/subscription-runtime')) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                observed_at: '2026-09-29T18:00:00Z',
                fresh_for_seconds: 45,
                workers: [],
                has_more: false,
              })
            )
          );
        }
        return Promise.resolve(new Response(JSON.stringify({})));
      })
    );

    render(<SubscriptionProfilesPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/Profile history unavailable/i);
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});
