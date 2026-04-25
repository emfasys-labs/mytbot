import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RootErrorBoundary } from './app/RootErrorBoundary';
import './styles/index.css';

// Redesigned "living instrument" shell lives under src/app/redesign/.
// Pass ?legacy=1 in the URL to load the previous production shell (src/app/App.tsx).
const useLegacy = (() => {
  try {
    return new URLSearchParams(window.location.search).get('legacy') === '1';
  } catch {
    return false;
  }
})();

function showFatalMessage(message: string, stack?: string) {
  const root = document.getElementById('root');
  if (!root) return;
  document.documentElement.classList.add('ds-root');
  root.innerHTML = '';
  const pre = document.createElement('pre');
  pre.style.cssText =
    'min-height:100vh;margin:0;padding:32px;background:#111113;color:#fda4af;font:14px/1.5 ui-monospace,monospace;white-space:pre-wrap;word-break:break-word';
  pre.textContent = `${message}${stack ? `\n\n${stack}` : ''}`;
  root.appendChild(pre);
}

async function bootstrap() {
  const el = document.getElementById('root');
  if (!el) {
    showFatalMessage('Missing <div id="root"> in index.html.');
    return;
  }

  try {
    if (useLegacy) {
      const { default: LegacyApp } = await import('./app/App');
      createRoot(el).render(
        <StrictMode>
          <RootErrorBoundary title="legacy shell error">
            <LegacyApp />
          </RootErrorBoundary>
        </StrictMode>,
      );
      return;
    }

    // Tag the root document so the design-system scoped styles apply.
    document.documentElement.classList.add('ds-root');
    const { default: RedesignApp } = await import('./app/redesign/App');
    createRoot(el).render(
      <StrictMode>
        <RootErrorBoundary title="Redesign shell error">
          <RedesignApp />
        </RootErrorBoundary>
      </StrictMode>,
    );
  } catch (e) {
    const err = e instanceof Error ? e : new Error(String(e));
    // eslint-disable-next-line no-console
    console.error('bootstrap failed', err);
    showFatalMessage(`Failed to load UI: ${err.message}\n\nTry: hard refresh (Ctrl+Shift+R).`, err.stack);
  }
}

void bootstrap();
