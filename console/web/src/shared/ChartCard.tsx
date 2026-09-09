import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';

export function ChartCard({ title, subtitle, right, children, className }: {
  title: string; subtitle?: string; right?: React.ReactNode;
  children: React.ReactNode; className?: string;
}) {
  return <div className={className}><Container header={
    <Header variant="h2" description={subtitle} actions={right}>{title}</Header>
  }>{children}</Container></div>;
}
