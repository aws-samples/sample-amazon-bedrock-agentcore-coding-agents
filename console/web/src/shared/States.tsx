import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Spinner from '@cloudscape-design/components/spinner';
import SpaceBetween from '@cloudscape-design/components/space-between';

export function LoadingState({ className }: { rows?: number; className?: string }) {
  return <div className={className} role="status"><div className="console-loading"><Spinner /> Loading data…</div></div>;
}
export function ErrorState({ error, className }: { error: string; className?: string }) {
  return <div className={className}><Alert type="error" header="Unable to load data">{error}</Alert></div>;
}
export function EmptyState({ title, hint, className }: { title: string; hint?: string; className?: string }) {
  return <div className={className}><div className="console-empty"><SpaceBetween size="xs">
    <Box fontWeight="bold">{title}</Box>
    {hint && <Box color="text-body-secondary">{hint}</Box>}
  </SpaceBetween></div></div>;
}
