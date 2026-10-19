import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import AgentsPage from './page';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

describe('AgentsPage', () => {
  it('renders role disclosure cards with human-formatted limits and secondary prompt digests', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === '/projects') {
        return [
          { id: 'proj-1', name: 'Parallel Forge', policy_version: 2 },
        ] as T;
      }
      if (path.startsWith('/projects/proj-1/policy-versions/')) {
        return {
          id: 'pol-1',
          project_id: 'proj-1',
          version: 2,
          created_at: '2026-10-16T12:00:00Z',
          document: {
            planner_model: {
              provider: 'google',
              model: 'gemini-3.8-flash',
              reasoning_effort: 'medium',
              max_input_tokens: 100000,
              max_output_tokens: 8000,
              max_tool_calls: 30,
              max_duration_seconds: 300,
              max_cost_minor: 150,
            },
            developer_model: {
              provider: 'openai',
              model: 'gpt-6-sol',
              reasoning_effort: 'high',
              max_input_tokens: 120000,
              max_output_tokens: 16000,
              max_tool_calls: 40,
              max_duration_seconds: 600,
              max_cost_minor: 300,
            },
            reviewer_model: {
              provider: 'anthropic',
              model: 'claude-opus-5-5',
              reasoning_effort: 'high',
              max_input_tokens: 200000,
              max_output_tokens: 32000,
              max_tool_calls: 20,
              max_duration_seconds: 1800,
              max_cost_minor: 500,
            },
          },
        } as T;
      }
      if (path === '/agent-prompts') {
        return [
          { role: 'planner', version: 'v1', digest: 'sha256:abc123planner' },
          { role: 'developer', version: 'v1', digest: 'sha256:def456developer' },
          { role: 'reviewer', version: 'v1', digest: 'sha256:789ghireviewer' },
        ] as T;
      }
      throw new Error(`Unexpected path: ${path}`);
    });

    render(<AgentsPage />);

    expect(screen.getByRole('heading', { level: 1, name: 'Agents & models' })).toBeInTheDocument();
    // Distinct link to CLI subscription profiles
    expect(screen.getByRole('link', { name: /Subscription profiles/i })).toHaveAttribute('href', '/subscription-profiles');

    // Wait for policy to load
    const planner = await screen.findByText('Planner');
    expect(planner).toBeInTheDocument();
    expect(screen.getByText('gemini-3.8-flash')).toBeInTheDocument();
    // Human duration: 300s -> 5m
    expect(screen.getByText('5m')).toBeInTheDocument();
    // 100,000 token formatted limit
    expect(screen.getByText('100,000 tokens')).toBeInTheDocument();

    // Prompt digests rendered in disclosure
    expect(screen.getByText(/Configured prompt digests/i)).toBeInTheDocument();
    expect(screen.getByText('sha256:abc123planner')).toBeInTheDocument();
  });

  it('defaults role disclosures to closed summaries and avoids duplicate budget strings in the summary line', async () => {
    render(<AgentsPage />);
    const planner = await screen.findByText('Planner');
    const plannerCard = planner.closest('details');
    expect(plannerCard).toBeInTheDocument();
    expect(plannerCard).not.toHaveAttribute('open');

    // Summary line contains role, model, and reasoning
    const summary = plannerCard!.querySelector('summary')!;
    expect(summary).toHaveTextContent('Planner');
    expect(summary).toHaveTextContent('gemini-3.8-flash');
    expect(summary).toHaveTextContent(/Reasoning:\s*medium/i);

    // Duplicated budget strings must NOT appear in the summary line
    expect(summary).not.toHaveTextContent('limit');
    expect(summary).not.toHaveTextContent('100,000 in');

    // Detailed limits are inside the body
    const body = plannerCard!.querySelector('.disclosure-body')!;
    expect(body).toHaveTextContent('100,000 tokens');
    expect(body).toHaveTextContent('5m');
  });
});
