import { useEffect, useState } from 'react';

/**
 * Recharts paints series colors as SVG attributes, where a bare `var(--token)`
 * does not resolve. So we read the console's HSL design tokens off the document
 * root once on mount (and again if the theme class flips) and hand back concrete
 * `hsl(...)` strings. Series pull from the brand `--chart-1..5` ramp (the
 * mesh-gradient stops); grid/axis pull from `--border` / `--muted-foreground`. This keeps every chart on the same neutral palette as the
 * rest of the UI (and following light/dark), instead of a hard-coded hex set.
 *
 * `series` is ordered strongest → faintest, so a primary line and its supporting
 * lines stay legible against the grid in either theme.
 */
export interface ChartTheme {
  series: string[];
  grid: string;
  axis: string;
  faint: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipText: string;
}

function readToken(styles: CSSStyleDeclaration, name: string, fallback: string): string {
  const raw = styles.getPropertyValue(name).trim();
  return raw ? `hsl(${raw})` : fallback;
}

function resolve(): ChartTheme {
  if (typeof window === 'undefined') {
    // SSR / test default: the brand chart ramp (mesh-gradient stops) on a
    // neutral grid/axis.
    return {
      series: ['hsl(212 100% 47%)', 'hsl(178 100% 44%)', 'hsl(270 67% 47%)', 'hsl(37 91% 55%)', 'hsl(330 100% 50%)'],
      grid: 'hsl(0 0% 90%)',
      axis: 'hsl(0 0% 45%)',
      faint: 'hsl(0 0% 96%)',
      tooltipBg: 'hsl(0 0% 100%)',
      tooltipBorder: 'hsl(0 0% 90%)',
      tooltipText: 'hsl(0 0% 9%)',
    };
  }
  const s = getComputedStyle(document.documentElement);
  const foreground = readToken(s, '--foreground', 'hsl(0 0% 9%)');
  const muted = readToken(s, '--muted-foreground', 'hsl(0 0% 45%)');
  const border = readToken(s, '--border', 'hsl(0 0% 90%)');
  const mutedBg = readToken(s, '--muted', 'hsl(0 0% 96%)');
  const card = readToken(s, '--card', 'hsl(0 0% 100%)');
  return {
    // Series follow the brand `--chart-1..5` ramp (the mesh-gradient stops) so
    // data viz carries the brand palette; grid/axis stay on the neutral tokens.
    series: [
      readToken(s, '--chart-1', 'hsl(212 100% 47%)'),
      readToken(s, '--chart-2', 'hsl(178 100% 44%)'),
      readToken(s, '--chart-3', 'hsl(270 67% 47%)'),
      readToken(s, '--chart-4', 'hsl(37 91% 55%)'),
      readToken(s, '--chart-5', 'hsl(330 100% 50%)'),
    ],
    grid: border,
    axis: muted,
    faint: mutedBg,
    tooltipBg: card,
    tooltipBorder: border,
    tooltipText: foreground,
  };
}

export function useChartTheme(): ChartTheme {
  const [theme, setTheme] = useState<ChartTheme>(resolve);

  useEffect(() => {
    setTheme(resolve());
    // The theme switcher toggles a class on <html>; re-resolve when it does so
    // charts repaint to the new palette without a reload.
    const obs = new MutationObserver(() => setTheme(resolve()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => obs.disconnect();
  }, []);

  return theme;
}
