import { render, screen } from '@testing-library/react';
import { expect, test } from 'vitest';
import SettingsPage from './page';

test('settings hub explains what each configuration surface controls and where it is edited', () => {
  render(<SettingsPage />);
  expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Project policies' })).toHaveAttribute('href', '/policies');
  expect(screen.getByText(/named commands/i)).toBeInTheDocument();
  expect(screen.getAllByText(/new policy version/i).length).toBeGreaterThan(0);
  expect(screen.getByRole('link', { name: 'Agents & models' })).toHaveAttribute('href', '/agents');
  expect(screen.getAllByText(/reasoning/i).length).toBeGreaterThan(0);
  expect(screen.getByRole('link', { name: 'Tool permissions' })).toHaveAttribute('href', '/tools');
  // Local CLI providers are operator-trusted and may expose native tools
  // beyond Forge's interface; the hub must not promise a fixed ceiling.
  expect(screen.getByText(/may expose native tools beyond/i)).toBeInTheDocument();
  expect(screen.getByText(/operator-trusted/i)).toBeInTheDocument();
  expect(screen.queryByText(/fixed tool set/i)).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Subscription profiles' })).toHaveAttribute('href', '/subscription-profiles');
  expect(screen.getByText(/immutable profile versions/i)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Usage & monitoring policies' })).toHaveAttribute('href', '/usage');
  expect(screen.getByText(/checkpoint mode/i)).toBeInTheDocument();
  expect(screen.getByText(/hard token\/context caps/i)).toBeInTheDocument();
  expect(screen.getByText(/edited per project on the project page/i)).toBeInTheDocument();
});
