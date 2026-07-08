import { Boxes } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

/** Sidebar brand block, neutral AgentCore workshop mark. Clicking it returns home. */
export function Brand() {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      onClick={() => navigate('/agents')}
      title="Back to home"
      className="flex w-full items-center gap-2.5 rounded-md px-1 py-0.5 text-left hover:bg-accent group-data-[collapsible=icon]:justify-center"
    >
      {/* The mesh-gradient mark is the one place the brand colour appears in the
          chrome, a small homage to the hero-scale gradient (DESIGN.md). */}
      <div className="flex size-7 shrink-0 items-center justify-center rounded-md bg-[linear-gradient(135deg,#007cf0,#7928ca_52%,#ff0080)] text-white shadow-sm">
        <Boxes className="size-4" />
      </div>
      <div className="flex min-w-0 flex-col leading-tight group-data-[collapsible=icon]:hidden">
        <span className="truncate text-[13px] font-semibold tracking-[-0.01em]">AgentCore</span>
        <span className="eyebrow truncate normal-case tracking-[0.04em] text-[10px]">Coding Agents</span>
      </div>
    </button>
  );
}
