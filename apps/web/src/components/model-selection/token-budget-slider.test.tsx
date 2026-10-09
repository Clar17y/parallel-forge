import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { useState } from 'react';
import { TokenBudgetSlider } from './token-budget-slider';

afterEach(cleanup);

test('uses published reference presets while preserving custom values above the reference until edited', async () => {
  const change = vi.fn();
  render(<TokenBudgetSlider label="Input tokens" dimension="input" value={2_000_000} provider="google" model="gemini-3.5-flash" onChange={change} />);
  expect(screen.getByText('2,000,000 tokens')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '25%' }));
  expect(change).toHaveBeenLastCalledWith(262_144);
  const exact = screen.getByLabelText('Exact value');
  await userEvent.click(exact);
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Input tokens exact value' }), { target: { value: '2000001' } });
  expect(change).toHaveBeenLastCalledWith(2_000_001);
});

test('unknown model has an adjustable honest range and native keyboard slider', async () => {
  const change = vi.fn();
  function Harness() {
    const [value, setValue] = useState(120);
    return <TokenBudgetSlider label="Output tokens" dimension="output" value={value} provider="custom" model="private-model" onChange={next => { change(next); setValue(next); }} />;
  }
  render(<Harness />);
  expect(screen.getByText('Budget range: 1,000,000 tokens')).toBeInTheDocument();
  await userEvent.click(screen.getByText('About limits'));
  expect(screen.getByText(/No published capacity is available/)).toBeVisible();
  const slider = screen.getByRole('slider', { name: 'Output tokens' });
  expect(slider).toHaveAttribute('max', '1000000');
  fireEvent.change(slider, { target: { value: '121' } });
  expect(change).toHaveBeenCalledWith(121);
  expect(slider).toHaveAttribute('type', 'range');
});

test('adjustable range presets use the displayed range while exact saved values remain untouched', async () => {
  const change = vi.fn();
  render(<TokenBudgetSlider label="Shared input tokens" dimension="input" value={2_000_000} rangeMax={1_000_000} onChange={change} />);
  expect(screen.getByRole('slider', { name: 'Shared input tokens' })).toHaveAttribute('max', '2000000');
  expect(screen.getByText('2,000,000 tokens', { exact: true })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '50%' }));
  expect(change).toHaveBeenLastCalledWith(1_000_000);
});

test('an unknown model can lower its range and presets follow the new scale', async () => {
  const change = vi.fn();
  render(<TokenBudgetSlider label="Private output" dimension="output" value={0} provider="custom" model="private" onChange={change} />);
  await userEvent.click(screen.getByLabelText('Exact value'));
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Private output range maximum' }), { target: { value: '200000' } });
  expect(screen.getByRole('slider', { name: 'Private output' })).toHaveAttribute('max', '200000');
  await userEvent.click(screen.getByRole('button', { name: '25%' }));
  expect(change).toHaveBeenLastCalledWith(50_000);
});

test('a large shared budget keeps a steady scale and repeatable presets while editing', async () => {
  function Harness() {
    const [value, setValue] = useState(100_000_000);
    return <TokenBudgetSlider label="Shared tokens" dimension="input" value={value} rangeMax={1_000_000} onChange={setValue} />;
  }
  render(<Harness />);
  const slider = screen.getByRole('slider', { name: 'Shared tokens' });
  fireEvent.change(slider, { target: { value: '99999999' } });
  expect(slider).toHaveAttribute('max', '100000000');
  await userEvent.click(screen.getByRole('button', { name: '50%' }));
  expect(slider).toHaveValue('50000000');
  await userEvent.click(screen.getByRole('button', { name: '50%' }));
  expect(slider).toHaveValue('50000000');
});

test('a known model keeps its expanded slider range while presets use its reference', async () => {
  function Harness() {
    const [value, setValue] = useState(2_000_000);
    return <TokenBudgetSlider label="Model tokens" dimension="input" value={value} provider="google" model="gemini-3.5-flash" onChange={setValue} />;
  }
  render(<Harness />);
  await userEvent.click(screen.getByRole('button', { name: '25%' }));
  expect(screen.getByRole('slider')).toHaveValue('262144');
  expect(screen.getByRole('slider')).toHaveAttribute('max', '2000000');
});

test('external larger values expand the scale and a model change starts the correct new scale', () => {
  const change = vi.fn();
  const control = (value: number, provider = 'custom', model = 'private') => <TokenBudgetSlider label="Updated tokens" dimension="output" value={value} provider={provider} model={model} onChange={change} />;
  const view = render(control(0));
  view.rerender(control(3_000_000));
  view.rerender(control(500));
  expect(screen.getByRole('slider')).toHaveAttribute('max', '3000000');
  view.rerender(control(500, 'google', 'gemini-3.8-flash'));
  expect(screen.getByRole('slider')).toHaveAttribute('max', '65536');
  expect(screen.getByRole('slider')).toHaveValue('500');
});
