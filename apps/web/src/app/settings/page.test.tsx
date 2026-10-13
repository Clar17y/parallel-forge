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
  expect(screen.getByText(/assigned by the Forge server|fixed tool set/i)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Subscription profiles' })).toHaveAttribute('href', '/subscription-profiles');
  expect(screen.getByText(/immutable profile versions/i)).toBeInTheDocument();
});
