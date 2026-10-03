/** Shared UI and canvas colors. Changing presentation never changes chart values. */
export const palette = {
  primary: '#1664FF',
  primaryHover: '#0E4BD2',
  ink: '#1F2329',
  secondary: '#4E5969',
  muted: '#667085',
  surface: '#FFFFFF',
  subtle: '#F5F7FA',
  hover: '#EBEEF2',
  selected: '#EAF2FF',
  line: '#E5E7EB',
  lineStrong: '#CDD2DA',
  disabledBg: '#EBEEF2',
  disabledInk: '#858D9A',
} as const;

export const themeTokens: Record<string, string> = {
  '--brand-ink': '#1B205E',
  '--brand-accent': '#009B9F',
  '--blue': palette.primary,
  '--blue-hover': palette.primaryHover,
  '--ink': palette.ink,
  '--text-secondary': palette.secondary,
  '--muted': palette.muted,
  '--surface': palette.surface,
  '--surface-subtle': palette.subtle,
  '--hover': palette.hover,
  '--selected': palette.selected,
  '--line': palette.line,
  '--line-strong': palette.lineStrong,
  '--composer-border': '#AFC4E4',
  '--disabled-bg': palette.disabledBg,
  '--disabled-ink': palette.disabledInk,
  '--focus-ring': '#1664FF1A',
  '--shadow-sm': '0 2px 12px #1F232908',
  '--border': 'var(--line)',
  '--sidebar': 'var(--surface-subtle)',
  '--green': 'var(--blue)', // Legacy component alias, not a second theme.
};

export const chartPalette = [palette.primary, '#1CA6A1', '#8A6BEA', '#EDAA45', '#EC7485', '#5DA4E8'];

export function applyTheme(root: HTMLElement) {
  for (const [name, value] of Object.entries(themeTokens)) root.style.setProperty(name, value);
}
