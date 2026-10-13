import { expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { getProviderCue, ProviderBadge } from './provider-badge';

test('Muse and Meta render the Meta provider identity', () => {
  expect(getProviderCue('muse')).toEqual({ tone: 'meta', label: 'Meta' });
  render(<ProviderBadge provider="muse" />);
  expect(screen.getByText('Meta').closest('.provider-badge')?.getAttribute('data-provider')).toBe('meta');
});
