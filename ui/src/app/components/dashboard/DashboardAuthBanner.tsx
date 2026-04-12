import { useState } from 'react';
import { setDashboardReadToken } from '../../lib/api';

type Props = {
  visible: boolean;
  onTokenSaved: () => void;
};

export function DashboardAuthBanner({ visible, onTokenSaved }: Props) {
  const [value, setValue] = useState('');
  const [err, setErr] = useState<string | null>(null);

  if (!visible) return null;

  const apply = () => {
    const t = value.trim();
    if (!t) {
      setErr('Paste the same value as server DASHBOARD_READ_TOKEN');
      return;
    }
    setErr(null);
    setDashboardReadToken(t);
    setValue('');
    onTokenSaved();
  };

  return (
    <div className="shrink-0 border-b border-amber-500/30 bg-amber-950/40 px-3 py-2 text-[11px] text-amber-100/95">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="font-medium uppercase tracking-wide text-amber-200/90">Snapshot API blocked (401)</span>
        <span className="text-amber-100/80 max-w-[520px]">
          Reads require <code className="text-zinc-300">X-Dashboard-Token</code> matching the API&apos;s{' '}
          <code className="text-zinc-300">DASHBOARD_READ_TOKEN</code>. Paste it here (stored in this browser only) or set{' '}
          <code className="text-zinc-300">VITE_DASHBOARD_READ_TOKEN</code> and rebuild the UI.
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input
          type="password"
          autoComplete="off"
          placeholder="Dashboard read token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="min-w-[200px] flex-1 max-w-md rounded-md border border-white/15 bg-black/40 px-2 py-1.5 font-mono text-xs text-white placeholder:text-zinc-600"
        />
        <button
          type="button"
          onClick={apply}
          className="rounded-md bg-amber-500/20 px-3 py-1.5 text-xs font-medium uppercase tracking-wide text-amber-100 hover:bg-amber-500/30"
        >
          Save & retry
        </button>
      </div>
      {err ? <div className="mt-1 text-rose-300/90">{err}</div> : null}
    </div>
  );
}
