import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
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

async function bootstrap() {
  if (useLegacy) {
    const { default: LegacyApp } = await import('./app/App');
    createRoot(document.getElementById('root')!).render(
      <StrictMode>
        <LegacyApp />
      </StrictMode>,
    );
    return;
  }

  // Tag the root document so the design-system scoped styles apply.
  document.documentElement.classList.add('ds-root');
  const { default: RedesignApp } = await import('./app/redesign/App');
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <RedesignApp />
    </StrictMode>,
  );
}

void bootstrap();
