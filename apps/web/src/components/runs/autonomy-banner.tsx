import type { components } from '@/lib/api/schema';
import { remediationOrigin } from './run-presentation';

export function AutonomyBanner({ projection }: { projection: components['schemas']['RunProjection'] }) {
  const state = projection.run.state;
  let message = 'No autonomous work is currently authorized.';
  if (state === 'CREATED' || state === 'PLANNING') message = 'Forge may prepare a plan. Implementation requires plan approval.';
  else if (['PREPARING_WORKTREE', 'IMPLEMENTING', 'VALIDATING', 'REVIEWING'].includes(state)) message = 'Forge may implement and check the approved plan locally. Publishing a pull request requires separate approval.';
  else if (state === 'REMEDIATING') {
    const origin = remediationOrigin(projection);
    if (origin === 'remote') {
      message = `Forge may repair the pull request and push the repaired candidate under the existing publication approval after fresh validation and review (${projection.budgets.remote_remediation_remaining} remote repair attempts remaining). Merging still requires exact human approval.`;
    } else if (origin === 'unrecorded') {
      message = 'Forge may repair recorded findings. The event history does not show whether this repair came from the pull request observation or from local findings. Merging still requires exact human approval.';
    } else {
      message = `Forge may repair local findings within the remaining ${projection.budgets.local_remediation_remaining} remediation attempts.`;
    }
  }
  else if (state === 'MONITORING_PR') message = `Forge may monitor the pull request and perform bounded remote repair (${projection.budgets.remote_remediation_remaining} attempts remaining). Merging requires exact human approval.`;
  else if (state === 'PUBLISHING_PR') message = 'Forge may publish the approved pull-request candidate.';
  else if (projection.available_commands.some(command => command.name.startsWith('approve_'))) message = `Awaiting human ${projection.next_gate ?? ''} approval for the exact current evidence.`;
  return <section aria-label="Current autonomy"><h2>Current authority</h2><p>{message}</p></section>;
}
