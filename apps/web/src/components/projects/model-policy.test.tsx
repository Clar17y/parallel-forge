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
