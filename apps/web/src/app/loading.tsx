import { ActivityStatus } from '@/components/ui/activity-status';

export default function Loading() {
  return (
    <div className="p-6 max-w-xl">
      <ActivityStatus
        tone="info"
        isExecuting={true}
        title="Loading workspace"
        description="Retrieving workspace state and project data…"
      />
    </div>
  );
}
