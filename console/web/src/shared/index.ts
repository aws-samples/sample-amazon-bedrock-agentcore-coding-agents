// Shared design-system layer for the console's stat surfaces. These primitives
// give Governance (Module 3) and the stat spots in Agents/Tasks (Modules 1 and 2) one
// vocabulary: headline metrics, framed charts, sortable tables, section headers,
// and load/error/empty states, plus the formatters and chart palette they share.

export { StatCard } from './StatCard';
export { ChartCard } from './ChartCard';
export { SectionHeader } from './SectionHeader';
export { SortableTh } from './SortableTh';
export { LoadingState, ErrorState, EmptyState } from './States';
export { useSortable } from './useSortable';
export type { SortDir, SortValue, SortableOptions } from './useSortable';
export { useChartTheme } from './useChartTheme';
export type { ChartTheme } from './useChartTheme';
export {
  fmtNum, fmtCompact, fmtUsd, fmtPct, fmtSeconds, fmtDate, fmtTime, maskHandle,
} from './format';
