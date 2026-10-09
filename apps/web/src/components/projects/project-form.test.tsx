import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProjectForm } from './project-form';
import { ApiError } from '@/lib/api/client';
import { defaultJev } from './jev-settings';

afterEach(cleanup);
async function identity() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText('Project name'), 'Parallel');
  await user.type(screen.getByLabelText('Repository path'), 'C:/code/parallel');
  await user.type(screen.getByLabelText('GitHub repository'), 'owner/repo');
  return user;
}

test('submits named argv and defaults database off with separate path lists', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} />);
  const user = await identity();
  for (const details of screen.getAllByText('Advanced model and budget')) await user.click(details);
  expect(screen.getByLabelText('Local remediation limit')).toHaveValue(3);
  expect(screen.getByLabelText('Remote remediation limit')).toHaveValue(3);
  await user.click(screen.getByRole('button', { name: 'Add command' }));
  await user.type(screen.getByLabelText('Command name'), 'test');
  await user.type(screen.getByLabelText('Executable'), 'npm');
  await user.click(screen.getByRole('button', { name: 'Add argument' }));
  await user.type(screen.getByLabelText('Argument 1'), 'test');
  await user.type(screen.getByLabelText('Allowed environment files'), '.env.example');
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({
    commands: [expect.objectContaining({ kind: 'test', name: 'test', argv: ['npm', 'test'] })],
    database: { enabled: false }, allowed_environment_files: ['.env.example'], secret_paths: ['.env', '.env.local'],
  }));
});

test('saves selected Google model while retaining exact custom project budgets', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} initial={{ planner_model: {
    provider: 'google', model: 'legacy-custom-model', max_input_tokens: 900000, max_output_tokens: 37000,
    max_tool_calls: 123, max_duration_seconds: 7400, max_cost_minor: 3333,
  } }} />);
  const user = await identity();
  await user.selectOptions(screen.getByLabelText('Planner model'), JSON.stringify(['google', 'gemini-3.5-flash']));
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save.mock.calls[0][0].planner_model).toEqual({
    provider: 'google', model: 'gemini-3.5-flash', max_input_tokens: 900000, max_output_tokens: 37000,
    max_tool_calls: 123, max_duration_seconds: 7400, max_cost_minor: 3333,
  });
});

test.each([
  ['custom-provider', 'custom-model-v9'],
  ['custom-provider', 'gemini-3.8-flash-medium'],
  ['google', ' gemini-3.8-flash-medium'],
  ['google', 'gemini-3.8-flash-medium '],
  ['google', 'gemini-3.8-flash-medium\n'],
])('round-trips an untouched custom model %s/%s and out-of-range owner budget', async (provider, model) => {
  const save = vi.fn().mockResolvedValue(undefined);
  const planner = { provider, model, max_input_tokens: 900000,
    max_output_tokens: 45000, max_tool_calls: 125, max_duration_seconds: 7400, max_cost_minor: 6000 };
  render(<ProjectForm onSave={save} initial={{ planner_model: planner }} />);
  await identity();
  await userEvent.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save.mock.calls[0][0].planner_model).toEqual(planner);
});

test('blocks incompatible saved reasoning in the real form and allows owner recovery to Automatic', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const planner = {
    provider: 'google', model: 'gemini-3-pro', reasoning_effort: 'medium' as const,
    max_input_tokens: 900000, max_output_tokens: 45000, max_tool_calls: 125,
    max_duration_seconds: 7400, max_cost_minor: 6000,
  };
  render(<ProjectForm onSave={save} initial={{ planner_model: planner }} />);
  const user = await identity();
  const reasoning = screen.getByLabelText('Planner reasoning strength') as HTMLSelectElement;
  expect(reasoning).toHaveValue('medium');
  expect(reasoning.validationMessage).toContain('not supported');

  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).not.toHaveBeenCalled();
  expect(screen.getByTestId('section-disclosure-api-models')).toHaveAttribute('open');

  await user.selectOptions(reasoning, '');
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][0].planner_model).toEqual({ ...planner, reasoning_effort: null });
});

test('changing a compatible model to an unsupported identity blocks submit and a compatible edit recovers', async () => {
  const save = vi.fn().mockRejectedValueOnce(new ApiError(422, 'validation', {
    'planner_model.reasoning_effort': 'Correct the planner reasoning setting.',
  })).mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} initial={{ planner_model: {
    provider: 'google', model: 'gemini-3.5-flash', reasoning_effort: 'medium',
    max_input_tokens: 900000, max_output_tokens: 45000, max_tool_calls: 125,
    max_duration_seconds: 7400, max_cost_minor: 6000,
  } }} />);
  const user = await identity();
  await user.click(screen.getByText(/API models & budgets/));
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(screen.getByText('Correct the planner reasoning setting.')).toBeInTheDocument();
  await user.click(screen.getAllByText('Advanced model and budget')[0]);
  const model = screen.getByLabelText('Planner custom model');
  await user.clear(model);
  await user.type(model, 'gemini-3-pro');
  const reasoning = screen.getByLabelText('Planner reasoning strength') as HTMLSelectElement;
  expect(reasoning.validationMessage).toContain('not supported');
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).toHaveBeenCalledTimes(1);

  await user.clear(model);
  await user.type(model, 'gemini-3.5-flash');
  expect(reasoning.validationMessage).toBe('');
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
  expect(save.mock.calls[1][0].planner_model).toEqual({
    provider: 'google', model: 'gemini-3.5-flash', reasoning_effort: 'medium',
    max_input_tokens: 900000, max_output_tokens: 45000, max_tool_calls: 125,
    max_duration_seconds: 7400, max_cost_minor: 6000,
  });
});

test('blocks CLI route model in None mode and recovers when owner selects supported base ID', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const planner = {
    provider: 'google', model: 'gemini-3.8-flash-medium', reasoning_effort: null,
    max_input_tokens: 900000, max_output_tokens: 45000, max_tool_calls: 125,
    max_duration_seconds: 7400, max_cost_minor: 6000,
  };
  render(<ProjectForm onSave={save} initial={{ planner_model: planner }} />);
  const user = await identity();
  const reasoning = screen.getByLabelText('Planner reasoning strength') as HTMLSelectElement;
  expect(reasoning.validationMessage).toContain('CLI composite identity');
  expect(screen.getByRole('option', { name: 'Custom / saved: gemini-3.8-flash-medium' })).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).not.toHaveBeenCalled();
  expect(screen.getByTestId('section-disclosure-api-models')).toHaveAttribute('open');

  await user.selectOptions(screen.getByLabelText('Planner model'), JSON.stringify(['google', 'gemini-3.8-flash']));
  expect(reasoning.validationMessage).toBe('');
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][0].planner_model).toEqual({
    provider: 'google', model: 'gemini-3.8-flash', reasoning_effort: null,
    max_input_tokens: 900000, max_output_tokens: 45000, max_tool_calls: 125,
    max_duration_seconds: 7400, max_cost_minor: 6000,
  });
});

test('blocks CLI route model in explicit mode and recovers when owner selects supported base ID', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const planner = {
    provider: 'google', model: 'gemini-3.8-flash-medium', reasoning_effort: 'medium' as const,
    max_input_tokens: 900000, max_output_tokens: 45000, max_tool_calls: 125,
    max_duration_seconds: 7400, max_cost_minor: 6000,
  };
  render(<ProjectForm onSave={save} initial={{ planner_model: planner }} />);
  const user = await identity();
  const reasoning = screen.getByLabelText('Planner reasoning strength') as HTMLSelectElement;
  expect(reasoning.validationMessage).toContain('CLI composite identity');
  expect(screen.getByRole('option', { name: 'Custom / saved: gemini-3.8-flash-medium' })).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).not.toHaveBeenCalled();
  expect(screen.getByTestId('section-disclosure-api-models')).toHaveAttribute('open');

  await user.selectOptions(screen.getByLabelText('Planner model'), JSON.stringify(['google', 'gemini-3.8-flash']));
  expect(reasoning.validationMessage).toBe('');
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][0].planner_model).toEqual({
    provider: 'google', model: 'gemini-3.8-flash', reasoning_effort: 'medium',
    max_input_tokens: 900000, max_output_tokens: 45000, max_tool_calls: 125,
    max_duration_seconds: 7400, max_cost_minor: 6000,
  });
});

test('keeps legacy Jev policy absent until an operator explicitly configures it', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} />);
  const user = await identity();
  expect(screen.getByLabelText('Configure Jev for this project')).not.toBeChecked();
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save.mock.calls[0][0]).not.toHaveProperty('jev');
});

test('Jev settings expose source consent and editable unpinned model alias', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} />);
  const user = await identity();
  await user.click(screen.getByLabelText('Configure Jev for this project'));
  expect(screen.getByLabelText('Jev mode')).toHaveValue('off');
  expect(screen.getByLabelText('Allow remote processing of bounded, redacted source excerpts')).not.toBeChecked();
  await user.clear(screen.getByLabelText('Jev model alias'));
  await user.type(screen.getByLabelText('Jev model alias'), 'custom-alias');
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save.mock.calls[0][0].jev).toMatchObject({ mode: 'off', model: 'custom-alias', allow_remote: false });
});

test('a project can return to its profile default while explicit Off remains available', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm policyOnly initial={{ jev: { ...defaultJev, mode: 'off' } }} onSave={save} />);
  const override = screen.getByLabelText('Configure Jev for this project');
  expect(override).toBeEnabled();
  await userEvent.click(screen.getByRole('button', { name: 'Create policy version' }));
  expect(save.mock.calls[0][0].jev.mode).toBe('off');
  await userEvent.click(override);
  await userEvent.click(screen.getByRole('button', { name: 'Create policy version' }));
  expect(JSON.parse(JSON.stringify(save.mock.calls[1][0]))).not.toHaveProperty('jev');
});

test('trusted host requires explicit warning acknowledgement and merge methods are constrained', async () => {
  render(<ProjectForm onSave={vi.fn()} />);
  const user = await identity();
  expect(screen.getByRole('option', { name: 'Trusted host · unsandboxed' })).toBeDisabled();
  await user.click(screen.getByLabelText(/I trust this project/));
  await user.selectOptions(screen.getByLabelText('Runner'), 'trusted_host');
  expect(screen.getByLabelText('Runner')).toHaveValue('trusted_host');
  await user.click(screen.getByLabelText(/I trust this project/));
  expect(screen.getByLabelText('Runner')).toHaveValue('docker');
  expect(screen.getByRole('group', { name: 'Allowed merge methods' }).querySelectorAll('input')).toHaveLength(3);
});

test('enabled database requires reference and key and disabling drops the reference', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} />);
  const user = await identity();
  await user.click(screen.getByLabelText('Provision a PostgreSQL database per worktree'));
  expect(screen.getByLabelText('Administrator secret reference')).toBeRequired();
  expect(screen.getByLabelText('Injected environment key')).toBeRequired();
  await user.type(screen.getByLabelText('Administrator secret reference'), 'secret://postgres/admin');
  await user.click(screen.getByLabelText('Provision a PostgreSQL database per worktree'));
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ database: { enabled: false } }));
});

test('server field errors preserve input without exposing exception details', async () => {
  render(<ProjectForm onSave={vi.fn().mockRejectedValue(new ApiError(422, 'private detail', { repository_path: 'Invalid value.' }))} />);
  const user = await identity();
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Repository path')).toHaveValue('C:/code/parallel');
  expect(screen.getByLabelText('Repository path')).toHaveAttribute('aria-invalid', 'true');
  expect(screen.queryByText('private detail')).not.toBeInTheDocument();
});

test('existing commands start collapsed with useful summaries while new commands open for editing', async () => {
  const existingCommand = {
    kind: 'test' as const,
    name: 'npm test',
    argv: ['npm', 'test'],
    timeout_seconds: 300,
    required: true,
    network_enabled: false,
    environment_keys: [],
  };
  render(<ProjectForm initial={{ commands: [existingCommand] }} onSave={vi.fn()} />);

  const existingDetails = screen.getByTestId('command-editor-disclosure-0');
  expect(existingDetails).not.toHaveAttribute('open');
  expect(screen.getByText('Command 1: npm test')).toBeInTheDocument();

  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Add command' }));

  const newDetails = screen.getByTestId('command-editor-disclosure-1');
  expect(newDetails).toHaveAttribute('open');
});

test('deleting a command preserves edited values and expansion of adjacent commands', async () => {
  const user = userEvent.setup();
  const cmd1 = {
    kind: 'test' as const,
    name: 'first-cmd',
    argv: ['npm', 'test'],
    timeout_seconds: 300,
    required: true,
    network_enabled: false,
    environment_keys: [],
  };
  const cmd2 = {
    kind: 'lint' as const,
    name: 'second-cmd',
    argv: ['npm', 'run', 'lint'],
    timeout_seconds: 120,
    required: false,
    network_enabled: false,
    environment_keys: [],
  };
  render(<ProjectForm initial={{ commands: [cmd1, cmd2] }} onSave={vi.fn()} />);

  // Open second command and edit its name
  const secondSummary = screen.getByText(/second-cmd/);
  await user.click(secondSummary);
  const secondDetails = screen.getByTestId('command-editor-disclosure-1');
  expect(secondDetails).toHaveAttribute('open');

  const secondNameInput = screen.getByDisplayValue('second-cmd');
  await user.clear(secondNameInput);
  await user.type(secondNameInput, 'edited-second-cmd');

  // Delete first command
  await user.click(screen.getByRole('button', { name: 'Remove command 1' }));

  // The remaining command (previously second) keeps its edited name and remains open
  expect(screen.getByDisplayValue('edited-second-cmd')).toBeInTheDocument();
  const remainingDetails = screen.getByTestId('command-editor-disclosure-0');
  expect(remainingDetails).toHaveAttribute('open');
});

test('starter command examples append with safe defaults and clear duplicate handling', async () => {
  const user = userEvent.setup();
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} />);
  await identity();

  // Add npm test starter example
  const npmTestBtn = screen.getByRole('button', { name: /Add "npm test"/i });
  await user.click(npmTestBtn);

  // Newly appended example is open for editing and has safe defaults
  const details = screen.getByTestId('command-editor-disclosure-0');
  expect(details).toHaveAttribute('open');
  expect(screen.getByDisplayValue('npm test')).toBeInTheDocument();
  expect(screen.getByLabelText('Allow network for this command')).not.toBeChecked();

  // Trying to add npm test again clearly indicates duplicate
  expect(screen.getByRole('button', { name: /Add "npm test"/i })).toBeDisabled();

  // Add python pytest example
  const pytestBtn = screen.getByRole('button', { name: /Add "pytest"/i });
  await user.click(pytestBtn);

  // Saves appended commands without replacing
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({
    commands: [
      expect.objectContaining({ name: 'npm test', argv: ['npm', 'test'], network_enabled: false }),
      expect.objectContaining({ name: 'pytest', argv: ['python', '-m', 'pytest', '-q'], network_enabled: false }),
    ],
  }));
});

test('server validation errors reveal affected collapsed command and section disclosures', async () => {
  const user = userEvent.setup();
  const cmd = {
    kind: 'test' as const,
    name: 'npm test',
    argv: ['npm', 'test'],
    timeout_seconds: 300,
    required: true,
    network_enabled: false,
    environment_keys: [],
  };
  const save = vi.fn().mockRejectedValue(new ApiError(422, 'Validation error', {
    'commands.0.name': 'Command name is invalid.',
    'database.admin_url_secret_reference': 'Admin secret is required.',
  }));

  render(<ProjectForm initial={{ commands: [cmd], database: { enabled: true, injected_environment_key: 'DATABASE_URL', admin_url_secret_reference: 'secret://db/admin' } }} onSave={save} />);
  await identity();

  // Commands and resources start collapsed
  const cmdDisclosure = screen.getByTestId('command-editor-disclosure-0');
  expect(cmdDisclosure).not.toHaveAttribute('open');

  const dbDisclosure = screen.getByTestId('section-disclosure-worktree-resources');
  expect(dbDisclosure).not.toHaveAttribute('open');

  await user.click(screen.getByRole('button', { name: 'Register project' }));

  // Both affected disclosures are revealed on validation error
  await screen.findByRole('alert');
  expect(cmdDisclosure).toHaveAttribute('open');
  expect(dbDisclosure).toHaveAttribute('open');
  expect(screen.getByText('Command name is invalid.')).toBeInTheDocument();
  expect(screen.getByText('Admin secret is required.')).toBeInTheDocument();
});

test('displays numbered setup sections and explains next subscription profile step', () => {
  render(<ProjectForm onSave={vi.fn()} />);

  // Numbered sections
  expect(screen.getByText(/1\. Repository & identity/i)).toBeInTheDocument();
  expect(screen.getByText(/2\. Runner & sandboxing/i)).toBeInTheDocument();
  expect(screen.getByText(/3\. Named commands/i)).toBeInTheDocument();

  // Inline guidance and examples
  expect(screen.getByText(/e\.g\. owner\/repo/i)).toBeInTheDocument();

  // Onboarding next step explanation
  expect(screen.getByText(/Subscription profiles configure CLI models and reasoning\. Configure API models, reasoning strength, and budgets here\./)).toBeInTheDocument();
  expect(screen.getByText(/Next step after registration: select a subscription profile/i)).toBeInTheDocument();
});

test('reveals every enclosing disclosure for native validation of a hidden model field', async () => {
  render(<ProjectForm onSave={vi.fn()} />);
  const input = screen.getByLabelText('Planner provider');
  const inner = input.closest('details')!;
  const outer = screen.getByTestId('section-disclosure-api-models');
  expect(inner).not.toHaveAttribute('open');
  expect(outer).not.toHaveAttribute('open');
  fireEvent.invalid(input);
  await waitFor(() => expect(outer).toHaveAttribute('open'));
  expect(inner).toHaveAttribute('open');
});

test('reveals every enclosing disclosure for server validation of a hidden model budget', async () => {
  const save = vi.fn().mockRejectedValue(new ApiError(422, 'Validation error', {
    'planner_model.max_duration_seconds': 'Duration is invalid.',
  }));
  render(<ProjectForm policyOnly onSave={save} />);
  await userEvent.click(screen.getByRole('button', { name: 'Create policy version' }));
  await screen.findByRole('alert');
  const input = screen.getByLabelText('Planner duration seconds');
  expect(input).toHaveAttribute('aria-invalid', 'true');
  await waitFor(() => expect(input.closest('details')).toHaveAttribute('open'));
  expect(screen.getByTestId('section-disclosure-api-models')).toHaveAttribute('open');
});
