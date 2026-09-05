/**
 * One source of truth for colour, spacing and type.
 *
 * Dark-first: this app is opened in kitchens, gyms and bedrooms at 6 a.m., and
 * a white screen at 6 a.m. is a hostile act. Light mode is a full palette swap,
 * not an inversion.
 */
import { useColorScheme } from 'react-native';

const palette = {
  // Semantic ramp used for macro rings and progress bars.
  protein: '#5B9CFF',
  carbs: '#F2A93B',
  fat: '#E8618C',
  fiber: '#4FC08D',
  water: '#3EC8E8',
};

export const dark = {
  ...palette,
  bg: '#0B0F14',
  surface: '#141A22',
  surfaceAlt: '#1C242E',
  border: '#28323E',
  text: '#EEF3F8',
  textDim: '#93A1B0',
  textFaint: '#5E6C7A',
  accent: '#5EE39A',
  accentText: '#04180E',
  danger: '#FF6B6B',
  warn: '#F5B942',
  success: '#5EE39A',
  overlay: 'rgba(4,8,12,0.72)',
};

export const light = {
  ...palette,
  bg: '#F6F8FB',
  surface: '#FFFFFF',
  surfaceAlt: '#EEF2F7',
  border: '#DDE4EC',
  text: '#0F1720',
  textDim: '#5A6876',
  textFaint: '#8F9BA8',
  accent: '#12A15F',
  accentText: '#FFFFFF',
  danger: '#D93A3A',
  warn: '#B87503',
  success: '#12A15F',
  overlay: 'rgba(255,255,255,0.75)',
};

export type Colors = typeof dark;

export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 } as const;
export const radius = { sm: 8, md: 14, lg: 20, pill: 999 } as const;

export const type = {
  hero: { fontSize: 40, fontWeight: '700' as const, letterSpacing: -1 },
  h1: { fontSize: 26, fontWeight: '700' as const, letterSpacing: -0.4 },
  h2: { fontSize: 20, fontWeight: '600' as const, letterSpacing: -0.2 },
  body: { fontSize: 15, fontWeight: '400' as const, lineHeight: 22 },
  label: { fontSize: 13, fontWeight: '600' as const, letterSpacing: 0.2 },
  caption: { fontSize: 12, fontWeight: '500' as const },
  mono: { fontSize: 13, fontVariant: ['tabular-nums'] as const },
};

export function useTheme(): Colors {
  return useColorScheme() === 'light' ? light : dark;
}

/** Confidence bands get a colour so the user reads accuracy without reading text. */
export function confidenceColor(c: Colors, confidence: number): string {
  if (confidence >= 0.78) return c.success;
  if (confidence >= 0.55) return c.warn;
  return c.danger;
}
