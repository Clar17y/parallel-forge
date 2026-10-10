import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test } from 'vitest';
import { PolicySummary } from './policy-summary';
import type { components } from '@/lib/api/schema';

afterEach(cleanup);

const samplePolicy: components['schemas']['ProjectPolicyResponse'] = {
  project_id: '11111111-1111-1111-1111-111111111111',
  version: 2,
  policy_version: 2,
  policy_digest: 'sha256:abc123456789',
  document_schema_version: 1,
  document: {
    runner_mode: 'docker',
    trusted_project: false,
    commands: [
      {
        kind: 'test',
        name: 'npm test',
        argv: ['npm', 'test', '--', '--watch=false'],
        required: true,
        network_enabled: false,
        timeout_seconds: 300,
        environment_keys: ['NODE_ENV'],
      },
      {
        kind: 'lint',
        name: 'run linter',
        argv: ['npm', 'run', 'lint'],
        required: false,
        network_enabled: true,
        timeout_seconds: 120,
        environment_keys: [],
      },
    ],
    database: {
      enabled: true,
      injected_environment_key: 'DATABASE_URL',
      admin_url_secret_reference: 'secret://db/admin',
    },
    allowed_environment_files: ['.env.test'],
    secret_paths: ['.env', '.env.local'],
    allowed_merge_methods: ['squash', 'rebase'],
    local_remediation_limit: 5,
    remote_remediation_limit: 2,
    jev: {
      mode: 'shadow',
      model: 'jev-fast',
      allow_remote: false,
    },
  },
};

test('renders grouped summaries with commands collapsed by default and displays summary badges', () => {
  render(<PolicySummary policy={samplePolicy} />);

  // Commands start collapsed
  const commandDetails = screen.getAllByTestId('command-summary-disclosure');
  expect(commandDetails).toHaveLength(2);
  expect(commandDetails[0]).not.toHaveAttribute('open');
  expect(commandDetails[1]).not.toHaveAttribute('open');

  // Command 1 summary details
  expect(screen.getByText('npm test')).toBeInTheDocument();
  expect(screen.getByText('kind: test')).toBeInTheDocument();
  expect(screen.getByText('npm test -- --watch=false')).toBeInTheDocument();
  expect(screen.getByText('Required')).toBeInTheDocument();
  expect(screen.getByText('No network')).toBeInTheDocument();
  expect(screen.getByText('300s timeout')).toBeInTheDocument();

  // Command 2 summary details
  expect(screen.getByText('run linter')).toBeInTheDocument();
  expect(screen.getByText('Optional')).toBeInTheDocument();
  expect(screen.getByText('Network allowed')).toBeInTheDocument();
  expect(screen.getByText('120s timeout')).toBeInTheDocument();
});

test('expanding command reveals exact argv order and details', async () => {
  const user = userEvent.setup();
  render(<PolicySummary policy={samplePolicy} />);

  const summary = screen.getByText('npm test');
  await user.click(summary);

  // Exact argv items with order
  expect(screen.getAllByText('Arg 0:')[0]).toBeInTheDocument();
  expect(screen.getAllByText('Arg 1:')[0]).toBeInTheDocument();
  expect(screen.getAllByText('Arg 2:')[0]).toBeInTheDocument();
  expect(screen.getByText('Arg 3:')).toBeInTheDocument();
  expect(screen.getByText('"--watch=false"')).toBeInTheDocument();
});

test('exact argument inspection preserves spaces, tabs, quotes and argument boundaries', async () => {
  const argv = ['node', 'fixtures/two  spaces.json', '  padded  ', 'a\tb', 'quoted"value'];
  render(<PolicySummary policy={{ ...samplePolicy, document: {
    commands: [{ kind: 'test', name: 'npm test', argv }],
  } }} />);
  const disclosure = screen.getByTestId('command-summary-disclosure');
  await userEvent.click(within(disclosure).getByText('npm test'));
  const encoded = [...disclosure.querySelectorAll('ol code')].map(item => item.textContent);
  expect(encoded).toEqual(argv.map(arg => JSON.stringify(arg)));
  expect(encoded.map(arg => JSON.parse(arg!))).toEqual(argv);
});

test('renders raw policy document under a collapsed disclosure', async () => {
  const user = userEvent.setup();
  render(<PolicySummary policy={samplePolicy} />);

  const rawDisclosure = screen.getByTestId('raw-document-disclosure');
  expect(rawDisclosure).not.toHaveAttribute('open');

  const rawSummary = screen.getByText('Raw policy document');
  await user.click(rawSummary);
  expect(rawDisclosure).toHaveAttribute('open');

  const pre = rawDisclosure.querySelector('pre');
  expect(pre).toBeInTheDocument();
  expect(pre?.textContent).toContain('sha256:abc123456789');
});

test('gracefully handles missing optional policy fields and unknown legacy fields without claiming absent values are off/zero', () => {
  const legacyPolicy: components['schemas']['ProjectPolicyResponse'] = {
    project_id: 'legacy-project',
    version: 1,
    policy_version: 1,
    policy_digest: 'sha256:legacy',
    document_schema_version: 0,
    document: {
      custom_legacy_setting: 'experimental-flag-123',
      unknown_feature: { nested: true },
    },
  };

  render(<PolicySummary policy={legacyPolicy} />);

  // Does not claim database or remediation are off or zero when completely absent
  expect(screen.queryByText(/database: disabled/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/remediation limit: 0/i)).not.toBeInTheDocument();

  // Displays unknown / legacy document fields safely
  expect(screen.getByText('custom_legacy_setting')).toBeInTheDocument();
  expect(screen.getByText('experimental-flag-123')).toBeInTheDocument();
});

test('never infers missing command permissions or database state from a partial policy', () => {
  render(<PolicySummary policy={{ ...samplePolicy, document: {
    commands: [{ name: 'Legacy command', argv: ['npm', 'test'] }],
    database: {},
  } }} />);
  expect(screen.queryByText('Optional')).not.toBeInTheDocument();
  expect(screen.queryByText('No network')).not.toBeInTheDocument();
  expect(screen.queryByText('Disabled')).not.toBeInTheDocument();
  expect(screen.getByText('Requirement unspecified')).toBeInTheDocument();
  expect(screen.getByText('Network unspecified')).toBeInTheDocument();
  expect(screen.getByText('Unspecified')).toBeInTheDocument();
});

test('shows model reasoning strength separately from inspectable exact token budgets', () => {
  render(<PolicySummary policy={{ ...samplePolicy, document: {
    planner_model: { provider: 'google', model: 'gemini-3.8-flash', reasoning_effort: 'high', max_input_tokens: 123456, max_output_tokens: 23456 },
  } }} />);
  expect(screen.getByText('Reasoning: high')).toBeInTheDocument();
  expect(screen.getByText('123456')).toBeInTheDocument();
  expect(screen.getByText('23456')).toBeInTheDocument();
  const modelDetails = screen.getByText('Reasoning: high').closest('details');
  expect(modelDetails).not.toHaveAttribute('open');
});

test.each([
  { name: 'custom severities', document: {
    publication_blocking_severities: ['minor', 'blocker'], merge_blocking_severities: ['major'],
  }, publication: 'minor, blocker', merge: 'major' },
  { name: 'empty severity sets', document: {
    publication_blocking_severities: [], merge_blocking_severities: [],
  }, publication: 'None', merge: 'None' },
  { name: 'partial policy', document: {
    publication_blocking_severities: ['major'],
  }, publication: 'major', merge: 'Unspecified' },
  { name: 'merge-only policy', document: {
    merge_blocking_severities: ['minor'],
  }, publication: 'Unspecified', merge: 'minor' },
  { name: 'legacy delivery settings', document: {
    allowed_merge_methods: ['squash'],
  }, publication: 'Unspecified', merge: 'Unspecified' },
])('shows delivery blocking severities without opening the raw document: $name', ({ document, publication, merge }) => {
  render(<PolicySummary policy={{ ...samplePolicy, document }} />);
  const delivery = screen.getByRole('heading', { name: 'Delivery & remediation' }).closest('section')!;
  expect(within(delivery).getByText('Publication blocking severities:').closest('span'))
    .toHaveTextContent(`Publication blocking severities: ${publication}`);
  expect(within(delivery).getByText('Merge blocking severities:').closest('span'))
    .toHaveTextContent(`Merge blocking severities: ${merge}`);
  expect(screen.getByTestId('raw-document-disclosure')).not.toHaveAttribute('open');
});
