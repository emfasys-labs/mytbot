/** Human labels for global-edge coordinator action kinds (snake_case from API). */
const KIND_LABELS: Record<string, string> = {
  open_strategy: 'Open strategy',
  trim_symbol: 'Trim / rotate',
};

export function formatCoordinatorKind(kind: string): string {
  const k = kind.trim();
  if (!k) return '—';
  return KIND_LABELS[k] ?? k.replace(/_/g, ' ');
}
