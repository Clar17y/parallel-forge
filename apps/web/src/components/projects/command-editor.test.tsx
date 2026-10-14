import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { expect, test } from 'vitest';
import type { components } from '@/lib/api/schema';
import { CommandEditor } from './command-editor';

type Command = components['schemas']['CommandSpec'];

const base: Command = {
  kind: 'test', name: 'test-focused', argv: ['.venv/bin/python', '-m', 'pytest', '-q'],
  timeout_seconds: 900, required: true, network_enabled: false, environment_keys: [],
};

function Harness({ onValue }: { onValue: (value: Command) => void }) {
  const [value, setValue] = useState<Command>(base);
  return <CommandEditor value={value} onChange={next => { setValue(next); onValue(next); }} />;
}

test('a command can be limited to the task\'s own test paths', async () => {
  let latest: Command = base;
  render(<Harness onValue={value => { latest = value; }} />);
  expect(screen.queryByLabelText('Focused path prefixes')).not.toBeInTheDocument();

  await userEvent.click(screen.getByLabelText(/Run only the task's owned paths/));
  const prefixes = screen.getByLabelText('Focused path prefixes');
  await userEvent.clear(prefixes);
  await userEvent.type(prefixes, 'apps/orchestrator/tests/{enter}tests/');
  expect(latest.path_scope?.prefixes).toEqual(['apps/orchestrator/tests/', 'tests/']);

  await userEvent.click(screen.getByLabelText(/Run only the task's owned paths/));
  expect(latest.path_scope).toBeNull();
});
