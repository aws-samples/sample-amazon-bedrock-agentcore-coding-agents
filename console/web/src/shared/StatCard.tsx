import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';

export function StatCard({ label, value, hint, trend, className }: {
  label: string; value: React.ReactNode; hint?: string;
  trend?: { pct: number; label?: string }; accent?: boolean; delay?: number; className?: string;
}) {
  return <div className={className}><Container><SpaceBetween size="xs">
    <Box color="text-label" fontWeight="bold">{label}</Box>
    <div className="console-metric-value">{value}</div>
    {trend && <Box color={trend.pct >= 0 ? 'text-status-success' : 'text-status-error'}>
      {trend.pct >= 0 ? '↑' : '↓'} {Math.abs(trend.pct).toFixed(1)}% {trend.label}
    </Box>}
    {hint && <Box color="text-body-secondary" fontSize="body-s">{hint}</Box>}
  </SpaceBetween></Container></div>;
}
