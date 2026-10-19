import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { Disclosure } from './disclosure';

describe('Disclosure component', () => {
  it('renders native details and summary with preserved children', () => {
    const { container } = render(
      <Disclosure
        id="test-disclosure"
        summary={<span data-testid="summary-content">Role details</span>}
        className="custom-class"
        defaultOpen
      >
        <div data-testid="child-content">
          <input data-testid="inner-input" defaultValue="draft text" />
        </div>
      </Disclosure>
    );

    const details = container.querySelector('details');
    expect(details).toBeInTheDocument();
    expect(details).toHaveAttribute('id', 'test-disclosure');
    expect(details).toHaveClass('disclosure-card', 'custom-class');
    expect(details).toHaveAttribute('open');

    const summary = container.querySelector('summary');
    expect(summary).toBeInTheDocument();
    expect(summary).toHaveClass('disclosure-summary');
    expect(screen.getByTestId('summary-content')).toHaveTextContent('Role details');

    const body = container.querySelector('.disclosure-body');
    expect(body).toBeInTheDocument();
    expect(screen.getByTestId('child-content')).toBeInTheDocument();
    expect(screen.getByTestId('inner-input')).toHaveValue('draft text');
  });

  it('keeps children mounted when closed without conditional unmount', () => {
    const { container } = render(
      <Disclosure summary="Closed summary">
        <input data-testid="mounted-input" defaultValue="unsaved draft" />
      </Disclosure>
    );

    const details = container.querySelector('details');
    expect(details).not.toHaveAttribute('open');
    // Child must still be present in the DOM for native collapse preservation
    const input = screen.getByTestId('mounted-input');
    expect(input).toBeInTheDocument();
    expect(input).toHaveValue('unsaved draft');
  });

  it('does not pass an unused defaultValue attribute to native details element', () => {
    const { container } = render(
      <Disclosure summary="Summary" defaultOpen>
        <span>Content</span>
      </Disclosure>
    );
    const details = container.querySelector('details');
    expect(details).not.toHaveAttribute('defaultValue');
  });

  it('retains edited input value when collapsed and reopened', () => {
    const { container } = render(
      <Disclosure summary="Summary" defaultOpen>
        <input data-testid="edit-input" defaultValue="initial" />
      </Disclosure>
    );
    const details = container.querySelector('details')!;
    const input = screen.getByTestId('edit-input') as HTMLInputElement;
    input.value = 'user edited text';

    // Simulate native collapse
    details.removeAttribute('open');
    expect(details).not.toHaveAttribute('open');
    expect(screen.getByTestId('edit-input')).toHaveValue('user edited text');

    // Simulate reopen
    details.setAttribute('open', '');
    expect(details).toHaveAttribute('open');
    expect(screen.getByTestId('edit-input')).toHaveValue('user edited text');
  });
});
