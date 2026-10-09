import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { defaultModel, ModelPolicy } from './model-policy';

afterEach(cleanup);

test('offers Google model presets and preserves custom identities and owner limits', async () => {
  const change = vi.fn();
  const custom = { ...defaultModel, provider: 'google-custom', model: 'inhouse-flash', max_input_tokens: 900000 };
  render(<ModelPolicy role="Planner" value={custom} onChange={change} />);
  const model = screen.getByLabelText('Planner model');
  expect(screen.getByRole('option', { name: 'Custom / saved: inhouse-flash' })).toBeInTheDocument();
  await userEvent.selectOptions(model, JSON.stringify(['google', 'gemini-3.5-flash']));
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ provider: 'google', model: 'gemini-3.5-flash', max_input_tokens: 900000 }));
  await userEvent.click(screen.getByText('Advanced model and budget'));
  expect(screen.getByLabelText('Planner provider')).toHaveValue('google-custom');
  expect(screen.getByRole('slider', { name: 'Planner input tokens' })).toHaveValue('900000');
  fireEvent.change(screen.getByRole('slider', { name: 'Planner input tokens' }), { target: { value: '1000000' } });
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ max_input_tokens: 1000000 }));
});

test('preserves a custom provider that shares a Google model ID until explicitly changed', async () => {
  const change = vi.fn();
  const custom = { ...defaultModel, provider: 'custom-provider', model: 'gemini-3.5-flash' };
  render(<ModelPolicy role="Planner" value={custom} onChange={change} />);
  const model = screen.getByLabelText('Planner model') as HTMLSelectElement;
  expect(model.value).toBe(JSON.stringify(['custom-provider', 'gemini-3.5-flash']));
  expect(screen.getByRole('option', { name: 'Custom / saved: gemini-3.5-flash' })).toBeInTheDocument();
  await userEvent.selectOptions(model, JSON.stringify(['google', 'gemini-3.5-flash']));
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ provider: 'google', model: 'gemini-3.5-flash' }));
});

test('uses token presets and exact values without changing the saved budget when the model changes', async () => {
  const change = vi.fn();
  render(<ModelPolicy role="Planner" value={{ ...defaultModel, max_input_tokens: 2_000_000 }} onChange={change} />);
  await userEvent.click(screen.getByText('Advanced model and budget'));
  expect(screen.getByText('2,000,000 tokens')).toBeInTheDocument();
  await userEvent.click(screen.getAllByRole('button', { name: '25%' })[0]);
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ max_input_tokens: 262_144 }));
  const slider = screen.getByRole('slider', { name: 'Planner input tokens' });
  fireEvent.change(slider, { target: { value: '300000' } });
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ max_input_tokens: 300000 }));
});

test('associates a project budget error with the corresponding slider', async () => {
  render(<ModelPolicy role="Planner" value={defaultModel} onChange={vi.fn()} errors={{ max_input_tokens: 'Input budget must be at least one' }} />);
  await userEvent.click(screen.getByText('Advanced model and budget'));
  const slider = screen.getByRole('slider', { name: 'Planner input tokens' });
  expect(slider).toHaveAttribute('aria-invalid', 'true');
  expect(slider).toHaveAttribute('aria-describedby');
  expect(document.getElementById(slider.getAttribute('aria-describedby')!)).toHaveTextContent('Input budget must be at least one');
});

test('renders visible reasoning strength selector outside advanced disclosure with choices', async () => {
  const change = vi.fn();
  render(<ModelPolicy role="Planner" value={defaultModel} onChange={change} />);
  const selector = screen.getByLabelText('Planner reasoning strength');
  expect(selector).toBeVisible();
  expect(screen.getByRole('option', { name: 'Default / automatic' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'Low (faster)' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'Medium (balanced)' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'High (deeper)' })).toBeInTheDocument();

  // Token limits explanation is visible outside advanced disclosure
  expect(screen.getByText(/Token limits cap usage while reasoning strength controls thinking\./)).toBeVisible();

  // Changing reasoning strength changes only reasoning_effort
  await userEvent.selectOptions(selector, 'high');
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({
    ...defaultModel,
    reasoning_effort: 'high',
  }));
});

test('explains preset thinking budgets for Gemini 2.5 models without altering output limits', () => {
  const value = { ...defaultModel, model: 'gemini-2.5-flash', max_output_tokens: 8192 };
  render(<ModelPolicy role="Developer" value={value} onChange={vi.fn()} />);
  expect(screen.getByText(/Preset thinking budgets for 2\.5: Low \(1,024 tokens\), Medium \(4,096 tokens\), High \(8,192 tokens\)\. Output token limits remain unchanged\./)).toBeVisible();
});

test('flags unsupported explicit reasoning on older Gemini 3 Pro and allows recovery to Automatic', async () => {
  const change = vi.fn();
  const custom = { ...defaultModel, provider: 'google', model: 'gemini-3.0-pro', reasoning_effort: 'medium' as const };
  render(<ModelPolicy role="Reviewer" value={custom} onChange={change} />);
  const selector = screen.getByLabelText('Reviewer reasoning strength');
  expect(selector).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByRole('option', { name: 'Unsupported: medium' })).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent(/Reasoning strength is not supported for model "gemini-3\.0-pro"/);

  // Switching to default/automatic removes error and updates reasoning_effort to null
  await userEvent.selectOptions(selector, '');
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({
    provider: 'google',
    model: 'gemini-3.0-pro',
    reasoning_effort: null,
  }));
});

test('flags CLI composite identity in ModelPolicy even with automatic reasoning', () => {
  const custom = { ...defaultModel, provider: 'google', model: 'gemini-3.8-flash-medium', reasoning_effort: null };
  render(<ModelPolicy role="Planner" value={custom} onChange={vi.fn()} />);
  const selector = screen.getByLabelText('Planner reasoning strength');
  expect(selector).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByRole('alert')).toHaveTextContent(/CLI composite identity/);
});

test('preserves custom and high precision token limits when changing reasoning', async () => {
  const change = vi.fn();
  const custom = {
    provider: 'google',
    model: 'gemini-3.8-flash',
    max_input_tokens: 1_800_000,
    max_output_tokens: 48_000,
    max_tool_calls: 42,
    max_duration_seconds: 1200,
    max_cost_minor: 500,
    reasoning_effort: null,
  };
  render(<ModelPolicy role="Planner" value={custom} onChange={change} />);
  const selector = screen.getByLabelText('Planner reasoning strength');
  await userEvent.selectOptions(selector, 'low');
  expect(change).toHaveBeenLastCalledWith({
    ...custom,
    reasoning_effort: 'low',
  });
});

test('preserves reasoning setting across model changes', async () => {
  const change = vi.fn();
  const policyWithReasoning = {
    ...defaultModel,
    model: 'gemini-3.5-flash',
    reasoning_effort: 'low' as const,
  };
  render(<ModelPolicy role="Developer" value={policyWithReasoning} onChange={change} />);
  const modelSelect = screen.getByLabelText('Developer model');
  await userEvent.selectOptions(modelSelect, JSON.stringify(['google', 'gemini-3.8-flash']));
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({
    provider: 'google',
    model: 'gemini-3.8-flash',
    reasoning_effort: 'low',
  }));
});

test('flags saved custom identity with unsupported reasoning and allows owner recovery', async () => {
  const change = vi.fn();
  const savedCustom = {
    ...defaultModel,
    provider: 'inhouse-provider',
    model: 'inhouse-specialist',
    reasoning_effort: 'high' as const,
  };
  render(<ModelPolicy role="Planner" value={savedCustom} onChange={change} />);
  const selector = screen.getByLabelText('Planner reasoning strength');
  expect(selector).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByRole('option', { name: 'Unsupported: high' })).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent(/Reasoning strength is not supported for provider "inhouse-provider"/);

  // Owner can recover by resetting to default/automatic
  await userEvent.selectOptions(selector, '');
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({
    provider: 'inhouse-provider',
    model: 'inhouse-specialist',
    reasoning_effort: null,
  }));
});

test('associates an explicit validation error with the reasoning selector', () => {
  render(
    <ModelPolicy
      role="Planner"
      value={defaultModel}
      onChange={vi.fn()}
      errors={{ reasoning_effort: 'Reasoning effort must be configured by an owner.' }}
    />
  );
  const selector = screen.getByLabelText('Planner reasoning strength');
  expect(selector).toHaveAttribute('aria-invalid', 'true');
  expect(selector).toHaveAttribute('aria-describedby');
  const errorEl = document.getElementById(selector.getAttribute('aria-describedby')!);
  expect(errorEl).toHaveTextContent('Reasoning effort must be configured by an owner.');
});
