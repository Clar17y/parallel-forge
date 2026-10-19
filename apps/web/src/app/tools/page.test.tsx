import { render, screen, within } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import ToolsPage from './page';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

describe('ToolsPage', () => {
  it('renders role-by-capability matrix, visible CLI boundary warning, and expandable exact tool names', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path.startsWith('/tool-permissions')) {
        return {
          items: [
            {
              role: 'planner',
              tools: [
                'repository.list_files',
                'repository.read_file',
                'repository.search',
                'repository.search_semantic',
                'repository.read_instructions',
              ],
            },
            {
              role: 'developer',
              tools: [
                'repository.list_files',
                'repository.read_file',
                'repository.search',
                'repository.search_semantic',
                'repository.read_instructions',
                'repository.write_file',
                'repository.delete_file',
                'repository.rename_file',
                'git.status',
                'git.diff',
                'git.commit',
                'build.run_named_check',
              ],
            },
            {
              role: 'reviewer',
              tools: [
                'repository.list_files',
                'repository.read_file',
                'repository.search',
                'repository.search_semantic',
                'repository.read_instructions',
                'git.status',
                'git.diff',
                'validation-results.read',
                'review-artifacts.read',
              ],
            },
          ],
          offset: 0,
          limit: 100,
          truncated: false,
        } as T;
      }
      throw new Error(`Unexpected path: ${path}`);
    });

    render(<ToolsPage />);

    expect(screen.getByRole('heading', { level: 1, name: 'Tool permissions' })).toBeInTheDocument();

    // Concise CLI boundary notice
    expect(screen.getByText(/Local CLI Provider Boundary/i)).toBeInTheDocument();
    expect(screen.getByText(/operator-trusted/i)).toBeInTheDocument();
    expect(screen.getByText(/approved-tools-unproved/i)).toBeInTheDocument();

    // Matrix table with capability headers
    const table = await screen.findByRole('table');
    expect(table).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: /role/i })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: /read/i })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: /write/i })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: /git/i })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: /checks/i })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: /review/i })).toBeInTheDocument();

    // Planner has Read but no Write
    const rows = within(table).getAllByRole('row');
    expect(rows.length).toBe(4); // 1 header + 3 roles

    // Exact tools are disclosed
    expect(screen.getAllByText('repository.read_file').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('build.run_named_check')).toBeInTheDocument();
  });
});
