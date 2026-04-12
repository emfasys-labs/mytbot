/**
 * UI-only mapping so tiny net scores (e.g. 0.002) read like meaningful conviction,
 * while values that already look like model scores (~0.5–1.0) pass through.
 */

export function clamp01(x: number): number {
  return Math.max(0, Math.min(1, x));
}

/** Map raw net / opportunity score to a 0–1 display conviction (for humans, not execution). */
export function displayConviction01(raw: number): number {
  const x = Number.isFinite(raw) ? raw : 0;
  const a = Math.abs(x);
  if (a > 0.35) return clamp01(x);
  return clamp01(0.5 + 0.5 * Math.tanh(x * 18));
}

export type StrengthBand = 'STRONG' | 'MEDIUM' | 'WEAK' | 'NOISE';

export function bandFromDisplay01(d: number): StrengthBand {
  if (d >= 0.7) return 'STRONG';
  if (d >= 0.45) return 'MEDIUM';
  if (d >= 0.22) return 'WEAK';
  return 'NOISE';
}

export function stanceFromDisplay01(d: number): 'HOLD' | 'WEAK' | 'STRONG' {
  if (d >= 0.62) return 'STRONG';
  if (d >= 0.38) return 'HOLD';
  return 'WEAK';
}

/** Two-decimal display score (0–1 scale). */
export function fmtScore01(d: number): string {
  return displayConviction01(d).toFixed(2);
}

/** Raw value for tooltips — avoids meaningless 0.0000 walls. */
export function fmtRawScore(raw: number): string {
  const x = Number.isFinite(raw) ? raw : 0;
  const a = Math.abs(x);
  if (a === 0) return '0';
  if (a >= 0.01) return x.toFixed(3);
  if (a >= 0.0001) return x.toFixed(4);
  return x.toExponential(2);
}

export function arrowForRaw(raw: number): string {
  if (raw > 0.0005) return '↑';
  if (raw < -0.0005) return '↓';
  return '·';
}

/** Emerald / rose intensity from 0–1 display conviction. */
export function convictionTextClass(display01: number, positive: boolean): string {
  if (positive) {
    if (display01 >= 0.72) return 'text-emerald-300';
    if (display01 >= 0.48) return 'text-emerald-400/90';
    return 'text-emerald-500/70';
  }
  if (display01 >= 0.72) return 'text-rose-300';
  if (display01 >= 0.48) return 'text-rose-400/90';
  return 'text-rose-500/70';
}
