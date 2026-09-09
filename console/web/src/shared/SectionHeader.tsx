import Header from '@cloudscape-design/components/header';
import StatusIndicator from '@cloudscape-design/components/status-indicator';

export function SectionHeader({ title, subtitle, right, source, note, className }: {
  title: string; subtitle?: string; eyebrow?: string; right?: React.ReactNode;
  source?: 'live' | 'ledger'; note?: string; className?: string;
}) {
  return <div className={className}><Header variant="h1" actions={right}
    description={subtitle} info={source ? <StatusIndicator type="info">
      {source === 'live' ? 'Runtime API' : 'Host ledger'}{note ? ` · ${note}` : ''}
    </StatusIndicator> : undefined}>{title}</Header></div>;
}
