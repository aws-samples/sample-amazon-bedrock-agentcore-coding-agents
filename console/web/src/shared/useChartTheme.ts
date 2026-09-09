import {
  colorChartsPaletteCategorical1, colorChartsPaletteCategorical2,
  colorChartsPaletteCategorical3, colorChartsPaletteCategorical4, colorChartsPaletteCategorical5,
  colorBorderDividerDefault, colorTextBodySecondary, colorBackgroundLayoutMain,
  colorBackgroundContainerContent, colorTextBodyDefault,
} from '@cloudscape-design/design-tokens';

export interface ChartTheme {
  series: string[];
  grid: string;
  axis: string;
  faint: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipText: string;
}
const theme: ChartTheme = {
  series: [colorChartsPaletteCategorical1, colorChartsPaletteCategorical2, colorChartsPaletteCategorical3,
    colorChartsPaletteCategorical4, colorChartsPaletteCategorical5],
  grid: colorBorderDividerDefault, axis: colorTextBodySecondary,
  faint: colorBackgroundLayoutMain, tooltipBg: colorBackgroundContainerContent,
  tooltipBorder: colorBorderDividerDefault, tooltipText: colorTextBodyDefault,
};

/** Native CSS color tokens follow Cloudscape light/dark mode in SVG and HTML. */
export function useChartTheme(): ChartTheme { return theme; }
