import Link from 'next/link';

/**
 * First-run guidance for a project. A step is only marked done from recorded
 * state: registration is proven by this page existing, the model choice by a
 * selected profile or by a run already having started, and the first run by
 * the run list. Unknown run history (`null`) never claims completion.
 */
export function SetupChecklist({ profileSelected, hasRuns }: {
  profileSelected: boolean;
  hasRuns: boolean | null;
}) {
  const modelsChosen = profileSelected || hasRuns === true;
  const firstRunDone = hasRuns === true;
  if (modelsChosen && firstRunDone) {
    return <section aria-label="Setup status">
      <p>Setup complete — this project is registered, has a way to run agents, and has started at least one run.</p>
    </section>;
  }
  const status = (done: boolean, isNext: boolean) => (done ? 'Done' : isNext ? 'Next' : 'To do');
  return <section aria-labelledby="setup-checklist-heading">
    <h2 id="setup-checklist-heading">Get set up</h2>
    <ol>
      <li><strong>Register your project</strong> — {status(true, false)}. Forge knows which repository to work in and what it may run.</li>
      <li><strong>Choose how agents run</strong> — {status(modelsChosen, !modelsChosen)}. {modelsChosen
        ? 'A subscription profile is selected for CLI models.'
        : <>Select a subscription profile below to choose CLI models and reasoning effort, or keep the API models in your project policy. <Link href="/subscription-profiles">Manage profiles</Link></>}</li>
      <li><strong>Start your first run</strong> — {status(firstRunDone, modelsChosen)}. {firstRunDone
        ? 'Runs have started in this project.'
        : <>Describe a task and Forge will plan it for your approval. <Link href="/runs/new">Start a run</Link></>}</li>
    </ol>
  </section>;
}
