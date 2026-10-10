import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, test } from 'vitest';
import { Button } from './button';

describe('Button', () => {
  afterEach(cleanup);

  test('preserves honest action label and sets aria-busy when busy', () => {
    render(<Button busy={true}>Submit proposal</Button>);
    const button = screen.getByRole('button', { name: 'Submit proposal' });
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button).toHaveTextContent('Submit proposal');
    const spinner = button.querySelector('svg.animate-spin');
    expect(spinner).toBeInTheDocument();
    expect(spinner).toHaveClass('motion-reduce:animate-none');
  });

  test('renders without spinner when not busy', () => {
    render(<Button busy={false}>Send message</Button>);
    const button = screen.getByRole('button', { name: 'Send message' });
    expect(button).not.toHaveAttribute('aria-busy');
    expect(button.querySelector('svg')).not.toBeInTheDocument();
  });
});
