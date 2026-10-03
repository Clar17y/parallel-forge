import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, test } from 'vitest';
import { parsePlan, PlanContent, PLAN_SECTIONS, type Plan } from './plan-content';

afterEach(() => {
  cleanup();
});

describe('parsePlan', () => {
  const validPlan: Plan = {
    summary: 'Build core feature',
    assumptions: ['Environment prepared'],
    affected_components: ['API layer'],
    steps: ['Implement service', 'Add tests'],
    required_checks: ['lint', 'test'],
    risks: ['Migration failure'],
    security_considerations: ['Enforce authentication'],
    dependency_changes: ['None'],
  };

  test('parses a valid legacy plan without owned_paths', () => {
    const result = parsePlan(JSON.stringify(validPlan));
    expect(result).toEqual(validPlan);
  });

  test('parses a valid plan with explicit owned_paths', () => {
    const scoped = { ...validPlan, owned_paths: ['src/services/plan.ts'] };
    const result = parsePlan(JSON.stringify(scoped));
    expect(result).toEqual(scoped);
  });

  test('accepts astral Unicode at backend text limits', () => {
    const unicodePlan = {
      ...validPlan,
      summary: '😀'.repeat(10000),
      ...Object.fromEntries(PLAN_SECTIONS.map(([key]) => [key, ['😀'.repeat(5000)]])),
    };

    expect(parsePlan(JSON.stringify(unicodePlan))).not.toBeNull();
  });

  test('accepts a summary mixing BMP and astral text at the code-point limit', () => {
    const summary = `${'a'.repeat(9999)}😀`;
    expect(parsePlan(JSON.stringify({ ...validPlan, summary }))).not.toBeNull();
  });

  test.each(['😀'.repeat(10001), `${'a'.repeat(10000)}😀`])(
    'rejects summaries beyond the code-point limit (%#)',
    (summary) => {
      expect(parsePlan(JSON.stringify({ ...validPlan, summary }))).toBeNull();
    },
  );

  test.each(PLAN_SECTIONS.map(([key]) => key))(
    'uses the backend code-point limit for %s items',
    (key) => {
      const atLimit = `${'😀'.repeat(4999)}é`;
      const beyondLimit = `${'😀'.repeat(5000)}é`;
      expect(parsePlan(JSON.stringify({ ...validPlan, [key]: [atLimit] }))).not.toBeNull();
      expect(parsePlan(JSON.stringify({ ...validPlan, [key]: [beyondLimit] }))).toBeNull();
    },
  );

  test.each(['a'.repeat(5001), '😀'.repeat(5001)])(
    'accepts writable paths without a frontend-only text limit (%#)',
    (path) => {
      const scoped = { ...validPlan, owned_paths: [path] };
      expect(parsePlan(JSON.stringify(scoped))).toEqual(scoped);
    },
  );

  test('rejects non-json or malformed structures', () => {
    expect(parsePlan('not json')).toBeNull();
    expect(parsePlan('123')).toBeNull();
    expect(parsePlan('[]')).toBeNull();
    expect(parsePlan('{}')).toBeNull();
  });

  test('rejects blank or oversized summary', () => {
    expect(parsePlan(JSON.stringify({ ...validPlan, summary: '' }))).toBeNull();
    expect(parsePlan(JSON.stringify({ ...validPlan, summary: '   ' }))).toBeNull();
    expect(parsePlan(JSON.stringify({ ...validPlan, summary: 'a'.repeat(10001) }))).toBeNull();
  });

  test('rejects malformed section lists or oversized items', () => {
    expect(parsePlan(JSON.stringify({ ...validPlan, steps: 'not-an-array' }))).toBeNull();
    expect(parsePlan(JSON.stringify({ ...validPlan, steps: ['a'.repeat(5001)] }))).toBeNull();
    expect(parsePlan(JSON.stringify({ ...validPlan, steps: Array(101).fill('step') }))).toBeNull();
    expect(parsePlan(JSON.stringify({ ...validPlan, steps: [123] }))).toBeNull();
  });

  test('rejects malformed owned_paths', () => {
    expect(parsePlan(JSON.stringify({ ...validPlan, owned_paths: 'not-array' }))).toBeNull();
    expect(parsePlan(JSON.stringify({ ...validPlan, owned_paths: [''] }))).toBeNull();
    expect(parsePlan(JSON.stringify({ ...validPlan, owned_paths: Array(65).fill('path') }))).toBeNull();
  });
});

describe('PlanContent', () => {
  const basePlan: Plan = {
    summary: 'Deploy new verification scheme',
    assumptions: ['Cluster ready'],
    affected_components: ['Auth worker'],
    steps: ['Draft spec', 'Review with team'],
    required_checks: ['verify-tokens'],
    risks: ['Downtime during switch'],
    security_considerations: ['<script>alert("xss")</script>'],
    dependency_changes: [],
  };

  test('renders summary, sections, and safely escapes untrusted text', () => {
    render(<PlanContent plan={basePlan} />);
    expect(screen.getByText('Deploy new verification scheme')).toBeInTheDocument();
    expect(screen.getByText('Draft spec')).toBeInTheDocument();
    expect(screen.getByText('verify-tokens')).toBeInTheDocument();
    expect(screen.getByText('<script>alert("xss")</script>')).toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText('None recorded')).toBeInTheDocument(); // dependency_changes was empty
    expect(screen.queryByRole('heading', { name: 'Writable paths' })).toBeNull();
  });

  test('renders explicit writable scope when declared', () => {
    render(<PlanContent plan={{ ...basePlan, owned_paths: ['apps/web/src', 'config.json'] }} />);
    expect(screen.getByRole('heading', { name: 'Writable paths' })).toBeInTheDocument();
    expect(screen.getByText('apps/web/src')).toBeInTheDocument();
    expect(screen.getByText('config.json')).toBeInTheDocument();
  });

  test('renders empty writable paths notice when owned_paths is empty array', () => {
    render(<PlanContent plan={{ ...basePlan, owned_paths: [] }} />);
    expect(screen.getByRole('heading', { name: 'Writable paths' })).toBeInTheDocument();
    expect(screen.getByText('No writable paths.')).toBeInTheDocument();
  });
});
