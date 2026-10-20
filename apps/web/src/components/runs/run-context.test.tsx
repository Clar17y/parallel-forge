import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { RunContext } from './run-context';
import { projection } from '@/test/projection';
import { ApiError, mutate } from '@/lib/api/client';

vi.mock('@/lib/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api/client')>(),
  mutate: vi.fn(),
  api: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.mocked(mutate).mockReset();
});

beforeEach(() => {
  vi.mocked(mutate).mockReset();
});

test('legacy runs display legacy workflow copy and do not expose preferences editor', () => {
  const p = projection();
  p.run.workflow = null;
  render(<RunContext projection={p} />);
  expect(screen.getByText('Legacy workflow (plan approval required)')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Edit preferences/i })).toBeNull();
});

test('ordinary runs display preferences and expose preferences editor', () => {
  const p = projection();
  p.run.workflow = {
    review_plan_first: false,
    delegation: 'auto',
    optional_review: 'auto',
  };
  render(<RunContext projection={p} />);
  expect(screen.getByText(/Ordinary \(prepare plan internally\)/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Edit preferences/i })).toBeInTheDocument();
});

test('terminal runs do not expose preferences editor', () => {
  const p = projection();
  p.run.workflow = {
    review_plan_first: false,
    delegation: 'auto',
    optional_review: 'auto',
  };
  p.run.state = 'COMPLETED';
  render(<RunContext projection={p} />);
  expect(screen.queryByRole('button', { name: /Edit preferences/i })).toBeNull();
});

test('editing preferences updates workflow via PUT /runs/{id}/workflow with CAS fencing', async () => {
  const user = userEvent.setup();
  const onChanged = vi.fn();
  const p = projection();
  p.run.version = 7;
  p.run.workflow = {
    review_plan_first: true,
    delegation: 'auto',
    optional_review: 'auto',
  };

  vi.mocked(mutate).mockResolvedValueOnce({
    id: 'run-1',
    workflow: {
      review_plan_first: false,
      delegation: 'disabled',
      optional_review: 'skip',
    },
  });

  render(<RunContext projection={p} onChanged={onChanged} />);
  await user.click(screen.getByRole('button', { name: /Edit preferences/i }));

  const checkbox = screen.getByLabelText(/Review plan before implementation/i);
  expect(checkbox).toBeChecked();
  await user.click(checkbox);
  expect(checkbox).not.toBeChecked();

  await user.selectOptions(screen.getByLabelText(/Delegation/i), 'disabled');
  await user.selectOptions(screen.getByLabelText(/Optional review/i), 'skip');
  await user.type(screen.getByLabelText(/Note/i), 'Direct execution preferred');

  await user.click(screen.getByRole('button', { name: /Save preferences/i }));

  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(1));
  expect(mutate).toHaveBeenCalledWith(
    '/runs/run-1/workflow',
    {
      expected_run_version: 7,
      expected_workflow: {
        review_plan_first: true,
        delegation: 'auto',
        optional_review: 'auto',
      },
      workflow: {
        review_plan_first: false,
        delegation: 'disabled',
        optional_review: 'skip',
      },
      note: 'Direct execution preferred',
    },
    expect.objectContaining({
      method: 'PUT',
      idempotencyKey: expect.any(String),
    })
  );

  expect(onChanged).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: /Save preferences/i })).toBeNull();
});

test('cancelling edit restores original view without mutating', async () => {
  const user = userEvent.setup();
  const p = projection();
  p.run.workflow = {
    review_plan_first: false,
    delegation: 'auto',
    optional_review: 'auto',
  };

  render(<RunContext projection={p} />);
  await user.click(screen.getByRole('button', { name: /Edit preferences/i }));
  expect(screen.getByRole('button', { name: /Save preferences/i })).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: /Cancel/i }));
  expect(screen.queryByRole('button', { name: /Save preferences/i })).toBeNull();
  expect(mutate).not.toHaveBeenCalled();
});

test('uncertain failure freezes exact request and key across retry', async () => {
  const user = userEvent.setup();
  const p = projection();
  p.run.version = 7;
  p.run.workflow = {
    review_plan_first: false,
    delegation: 'auto',
    optional_review: 'auto',
  };

  vi.mocked(mutate)
    .mockRejectedValueOnce(new Error('connection timeout'))
    .mockResolvedValueOnce({ id: 'run-1', workflow: p.run.workflow });

  render(<RunContext projection={p} />);
  await user.click(screen.getByRole('button', { name: /Edit preferences/i }));
  await user.click(screen.getByRole('button', { name: /Save preferences/i }));

  await screen.findByRole('alert');
  expect(screen.getByText(/Failed to update workflow preferences/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/Review plan before implementation/i)).toBeDisabled();
  expect(screen.getByLabelText(/Delegation/i)).toBeDisabled();
  expect(screen.getByLabelText(/Optional review/i)).toBeDisabled();
  expect(screen.getByLabelText(/Note/i)).toBeDisabled();

  // Retry button is available and retrying reuses exact key and payload
  await user.click(screen.getByRole('button', { name: /Retry update/i }));
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2));

  expect(mutate).toHaveBeenNthCalledWith(1, '/runs/run-1/workflow', expect.any(Object), expect.any(Object));
  expect(mutate).toHaveBeenNthCalledWith(2, '/runs/run-1/workflow', vi.mocked(mutate).mock.calls[0][1], vi.mocked(mutate).mock.calls[0][2]);
});

test('a definitive rejection keeps the draft editable and creates a new request after correction', async () => {
  const user = userEvent.setup();
  const p = projection();
  p.run.workflow = { review_plan_first: false, delegation: 'auto', optional_review: 'auto' };
  vi.mocked(mutate)
    .mockRejectedValueOnce(new ApiError(422, 'invalid-workflow'))
    .mockResolvedValueOnce({ id: p.run.id });
  render(<RunContext projection={p} />);
  await user.click(screen.getByRole('button', { name: /Edit preferences/i }));
  await user.type(screen.getByLabelText(/Note/i), 'Retain this draft');
  await user.click(screen.getByRole('button', { name: /Save preferences/i }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/rejected/i);
  expect(screen.getByLabelText(/Note/i)).toHaveValue('Retain this draft');
  expect(screen.getByLabelText(/Delegation/i)).toBeEnabled();
  await user.selectOptions(screen.getByLabelText(/Delegation/i), 'disabled');
  await user.click(screen.getByRole('button', { name: /Save preferences/i }));
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2));
  expect(vi.mocked(mutate).mock.calls[1][1]).toMatchObject({
    workflow: { delegation: 'disabled' }, note: 'Retain this draft',
  });
  expect(vi.mocked(mutate).mock.calls[1][2].idempotencyKey).not.toBe(vi.mocked(mutate).mock.calls[0][2].idempotencyKey);
});

test('stale 409 conflict retains user draft and calls refresh honestly', async () => {
  const user = userEvent.setup();
  const onChanged = vi.fn();
  const p = projection();
  p.run.version = 7;
  p.run.workflow = {
    review_plan_first: false,
    delegation: 'auto',
    optional_review: 'auto',
  };

  vi.mocked(mutate).mockRejectedValueOnce(new ApiError(409, 'stale-projection'));

  const view = render(<RunContext projection={p} onChanged={onChanged} />);
  await user.click(screen.getByRole('button', { name: /Edit preferences/i }));

  await user.selectOptions(screen.getByLabelText(/Delegation/i), 'disabled');
  await user.type(screen.getByLabelText(/Note/i), 'Unsaved custom note');

  await user.click(screen.getByRole('button', { name: /Save preferences/i }));

  await screen.findByRole('alert');
  expect(screen.getByText(/Run version or workflow preferences have changed concurrently/i)).toBeInTheDocument();
  // onChanged is called to refresh cockpit
  expect(onChanged).toHaveBeenCalledTimes(1);

  // Draft is retained in form!
  expect(screen.getByLabelText(/Delegation/i)).toHaveValue('disabled');
  expect(screen.getByLabelText(/Note/i)).toHaveValue('Unsaved custom note');

  // When refreshed projection arrives with new version
  const updatedProjection = projection();
  updatedProjection.run.version = 8;
  updatedProjection.run.workflow = {
    review_plan_first: false,
    delegation: 'requested',
    optional_review: 'auto',
  };
  view.rerender(<RunContext projection={updatedProjection} onChanged={onChanged} />);

  // The draft is still retained:
  expect(screen.getByLabelText(/Delegation/i)).toHaveValue('disabled');
  expect(screen.getByLabelText(/Note/i)).toHaveValue('Unsaved custom note');

  // Submitting again uses refreshed expectations and new key:
  vi.mocked(mutate).mockResolvedValueOnce({ id: 'run-1', workflow: updatedProjection.run.workflow });
  await user.click(screen.getByRole('button', { name: /Save preferences/i }));

  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2));
  expect(mutate).toHaveBeenNthCalledWith(2,
    '/runs/run-1/workflow',
    expect.objectContaining({
      expected_run_version: 8,
      expected_workflow: updatedProjection.run.workflow,
      workflow: expect.objectContaining({
        delegation: 'disabled',
      }),
      note: 'Unsaved custom note',
    }),
    expect.objectContaining({
      method: 'PUT',
      idempotencyKey: expect.any(String),
    })
  );
  expect(vi.mocked(mutate).mock.calls[1][2].idempotencyKey).not.toBe(vi.mocked(mutate).mock.calls[0][2].idempotencyKey);
});
