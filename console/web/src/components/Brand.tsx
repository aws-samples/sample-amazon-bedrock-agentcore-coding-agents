import { Boxes } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

/** Sidebar brand block, neutral AgentCore workshop mark. Clicking it returns home. */
export function Brand() {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      onClick={() => navigate('/development')}
      title="Back to home"
      className="flex w-full items-center gap-3 rounded-md px-1 py-1.5 text-left hover:bg-accent group-data-[collapsible=icon]:justify-center"
    >
      <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-[linear-gradient(135deg,#136e7a,#5188aa_52%,#8d77b2)] text-white shadow-sm">
        <Boxes aria-hidden="true" className="size-[18px]" />
      </div>
      <div className="flex min-w-0 flex-col leading-tight group-data-[collapsible=icon]:hidden">
        <span className="truncate text-sm font-semibold tracking-[-0.02em]">AgentCore</span>
        <span className="mt-0.5 truncate text-[11px] text-muted-foreground">Coding agents</span>
      </div>
    </button>
  );
}
