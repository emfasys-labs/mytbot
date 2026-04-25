import { Component, type ErrorInfo, type ReactNode } from 'react';

type Props = { children: ReactNode; title?: string };

type State = { err: Error | null };

/**
 * Catches render errors in the live shell. Without this, React 18+ may leave
 * a black ``#root`` (design-system background) when a child throws.
 */
export class RootErrorBoundary extends Component<Props, State> {
  state: State = { err: null };

  static getDerivedStateFromError(err: Error): State {
    return { err };
  }

  componentDidCatch(err: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('RootErrorBoundary', err, info.componentStack);
  }

  render() {
    const { err } = this.state;
    if (!err) return this.props.children;
    return (
      <div
        style={{
          minHeight: '100vh',
          background: '#111113',
          color: '#fda4af',
          padding: 32,
          fontFamily: "ui-monospace, 'Cascadia Code', monospace",
          fontSize: 14,
          lineHeight: 1.5,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        <h1 style={{ color: '#f87171', fontSize: 18, margin: '0 0 12px' }}>
          {this.props.title ?? 'UI error'}
        </h1>
        {String(err?.message || err)}
        {err?.stack && (
          <div style={{ marginTop: 16, color: 'rgba(255,255,255,0.5)', fontSize: 12 }}>{err.stack}</div>
        )}
        <p style={{ marginTop: 24, color: 'rgba(255,255,255,0.4)', fontSize: 12 }}>
          Open DevTools → Console for the full message. If this appeared after a git pull, try a hard
          refresh (Ctrl+Shift+R) or clear site data for this origin.
        </p>
      </div>
    );
  }
}
