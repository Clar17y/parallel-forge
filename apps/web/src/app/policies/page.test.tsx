import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import PoliciesPage from './page';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

describe('PoliciesPage', () => {
  it('renders project policy browser with version heading and compact digest disclosure', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === '/projects') {
        return [
          { id: 'proj-1', name: 'Parallel Forge', policy_version: 1 },
        ] as T;
      }
      if (path.startsWith('/projects/proj-1/policy-versions/')) {
        return {
          id: 'pol-1',
          project_id: 'proj-1',
          version: 1,
          policy_digest: 'sha256:11223344556677889900aabbccddeeff',
          document_schema_version: 1,
          document: {
            runner_mode: 'docker',
            trusted_project: false,
            commands: [],
          },
        } as T;
      }
      throw new Error(`Unexpected path: ${path}`);
    });

    render(<PoliciesPage />);

    expect(screen.getByRole('heading', { level: 1, name: /policies/i })).toBeInTheDocument();

    const versionHeading = await screen.findByRole('heading', { level: 2, name: /Parallel Forge · Version 1/i });
    expect(versionHeading).toBeInTheDocument();

    expect(screen.getByRole('link', { name: /create next policy version/i })).toHaveAttribute(
      'href',
      '/projects/proj-1'
    );

    // Digest is tucked into a disclosure
    expect(screen.getByText(/Digest: sha256:/i)).toBeInTheDocument();
  });
});
