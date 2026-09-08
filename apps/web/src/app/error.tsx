"use client";
export default function Error({ reset }: { reset: () => void }) {
  return <section role="alert"><h2>Workspace unavailable</h2>
    <button onClick={reset}>Try again</button>
  </section>;
}
