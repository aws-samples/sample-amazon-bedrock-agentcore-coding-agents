import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { cn } from '@foxl/ui';
import type { RunDiff } from '../api';

export function BuildSnapshot({ snapshot }: { snapshot: RunDiff }) {
  if (snapshot.files.length === 0) return null;
  return <Container header={<Header variant="h2" counter={`(${snapshot.files.length})`}
    description="The full recorded repository snapshot, including unchanged files. These patches start from an empty snapshot. Open a pull request to review its changes.">Build snapshot</Header>}>
    <SpaceBetween size="s">{snapshot.files.map(file => <ExpandableSection key={file.path} headerText={file.path}
      defaultExpanded={snapshot.files.length <= 2}><DiffBody patch={file.patch} /></ExpandableSection>)}</SpaceBetween>
  </Container>;
}

// Render the recorded git-show patch without interpreting its source as HTML.
function DiffBody({ patch }: { patch: string }) {
  const lines = patch.split('\n').filter(
    (line) => !/^(diff --git |index |--- |\+\+\+ |new file|deleted file|similarity |rename )/.test(line));
  return (
    <pre className="max-h-72 overflow-auto bg-[#1e1e1e] px-3 py-2 font-mono text-[11.5px] leading-relaxed [scrollbar-width:thin] [scrollbar-color:#555_transparent]">
      {lines.map((line, i) => {
        const tone = line.startsWith('@@')
          ? 'text-sky-400'
          : line.startsWith('+')
            ? 'bg-emerald-500/10 text-emerald-300'
            : line.startsWith('-')
              ? 'bg-destructive/10 text-red-300'
              : 'text-[#d4d4d4]';
        return <div key={i} className={cn('whitespace-pre-wrap break-all px-1', tone)}>{line || ' '}</div>;
      })}
    </pre>
  );
}
