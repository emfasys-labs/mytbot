import { AnimatePresence, motion } from 'motion/react';
import { Loader2, Power } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { SystemState } from '../lib/api';

type ControlState = 'live' | 'pause' | 'flatten';

interface MasterControlProps {
  currentState: ControlState;
  systemState: SystemState;
  onStateChange: (state: ControlState) => void;
  onSystemStart: () => Promise<void>;
  onSystemStop: () => Promise<void>;
  onHaptic?: () => void;
}

const BTN = 44;
const TRACK_H = 96;
const HOLD_MS = 800;
const WHEEL_STEP = 0.12;

export function MasterControl({
  currentState,
  systemState,
  onStateChange,
  onSystemStart,
  onSystemStop,
  onHaptic,
}: MasterControlProps) {
  const [hintText, setHintText] = useState<string | null>(null);
  const [isTransitioning, setIsTransitioning] = useState(false);
  const [armed, setArmed] = useState(false);
  const [slideProgress, setSlideProgress] = useState(0);

  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pointerDown = useRef(false);
  const longPressTriggered = useRef(false);
  const touchStartY = useRef(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const progressRef = useRef(0);

  const isSystemOff = systemState === 'off' || systemState === 'error';
  const isSystemStarting = systemState === 'starting';
  const isSystemStopping = systemState === 'stopping';
  const isSystemRunning = systemState === 'running';
  const isBusy = isTransitioning || isSystemStarting || isSystemStopping;

  useEffect(() => {
    return () => {
      if (longPressTimer.current) clearTimeout(longPressTimer.current);
      if (hintTimer.current) clearTimeout(hintTimer.current);
    };
  }, []);

  const flashHint = (text: string) => {
    setHintText(text);
    if (hintTimer.current) clearTimeout(hintTimer.current);
    hintTimer.current = setTimeout(() => setHintText(null), 2000);
  };

  const clearLongPress = () => {
    if (longPressTimer.current) clearTimeout(longPressTimer.current);
    longPressTimer.current = null;
  };

  const doStart = async () => {
    if (isBusy) return;
    setIsTransitioning(true);
    flashHint('Starting...');
    try {
      await onSystemStart();
      flashHint('System ON');
    } catch {
      flashHint('Start failed');
    } finally {
      setIsTransitioning(false);
    }
  };

  const doStop = useCallback(async () => {
    if (isBusy) return;
    setIsTransitioning(true);
    setArmed(false);
    setSlideProgress(0);
    progressRef.current = 0;
    flashHint('Stopping...');
    try {
      await onSystemStop();
      onStateChange('flatten');
      flashHint('System OFF');
    } catch {
      flashHint('Stop failed');
    } finally {
      setIsTransitioning(false);
    }
  }, [isBusy, onSystemStop, onStateChange]);

  const disarm = () => {
    setArmed(false);
    setSlideProgress(0);
    progressRef.current = 0;
  };

  const pushProgress = useCallback(
    (next: number) => {
      const clamped = Math.max(0, Math.min(1, next));
      progressRef.current = clamped;
      setSlideProgress(clamped);
      if (clamped >= 1) {
        doStop();
      }
    },
    [doStop],
  );

  /* ── Button tap / long-press (works on both desktop and mobile) ── */

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (isBusy) return;
    if (armed) return;

    e.currentTarget.setPointerCapture(e.pointerId);
    pointerDown.current = true;
    longPressTriggered.current = false;

    clearLongPress();
    longPressTimer.current = setTimeout(() => {
      if (!pointerDown.current) return;
      longPressTriggered.current = true;
      onHaptic?.();

      if (isSystemOff) {
        doStart();
      } else if (isSystemRunning) {
        setArmed(true);
        setSlideProgress(0);
        progressRef.current = 0;
      }
    }, HOLD_MS);
  };

  const onPointerUp = () => {
    clearLongPress();

    if (armed) return;

    if (!longPressTriggered.current) {
      if (isSystemOff) {
        doStart();
      } else if (isSystemRunning) {
        if (currentState === 'live') {
          onStateChange('pause');
          onHaptic?.();
          flashHint('Paused');
        } else if (currentState === 'pause' || currentState === 'flatten') {
          onStateChange('live');
          onHaptic?.();
          flashHint('Live');
        }
      } else if (isBusy) {
        flashHint(isSystemStarting ? 'Starting...' : 'Stopping...');
      }
    }

    pointerDown.current = false;
  };

  const onPointerCancel = () => {
    clearLongPress();
    pointerDown.current = false;
  };

  /* ── Slider: mouse wheel (desktop) ── */

  const onWheel = useCallback(
    (e: React.WheelEvent<HTMLDivElement>) => {
      if (!armed) return;
      e.stopPropagation();
      const delta =
        Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      const direction = delta > 0 ? 1 : -1;
      const magnitude = Math.min(1, Math.abs(delta) / 120);
      pushProgress(progressRef.current + direction * WHEEL_STEP * magnitude);
    },
    [armed, pushProgress],
  );

  /* ── Slider: touch drag (mobile) ── */

  const onTouchStart = useCallback(
    (e: React.TouchEvent<HTMLDivElement>) => {
      if (!armed) return;
      touchStartY.current = e.touches[0].clientY;
    },
    [armed],
  );

  const onTouchMove = useCallback(
    (e: React.TouchEvent<HTMLDivElement>) => {
      if (!armed) return;
      const dy = e.touches[0].clientY - touchStartY.current;
      const pct = Math.max(0, Math.min(1, dy / TRACK_H));
      pushProgress(pct);
    },
    [armed, pushProgress],
  );

  const onTouchEnd = useCallback(() => {
    if (!armed) return;
    if (progressRef.current < 1) {
      disarm();
    }
  }, [armed]);

  /* ── Click outside to disarm ── */

  useEffect(() => {
    if (!armed) return;
    const handler = (e: PointerEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) disarm();
    };
    window.addEventListener('pointerdown', handler);
    return () => window.removeEventListener('pointerdown', handler);
  }, [armed]);

  const getStateColor = () => {
    if (isBusy) {
      return {
        bg: 'bg-blue-400/10',
        text: 'text-blue-300',
        glow: 'rgba(96, 165, 250, 0.22)',
        border: 'border-blue-300/30',
      };
    }
    if (isSystemOff) {
      return {
        bg: 'bg-gray-400/10',
        text: 'text-gray-400',
        glow: 'rgba(156, 163, 175, 0.12)',
        border: 'border-gray-400/30',
      };
    }
    if (armed) {
      return {
        bg: 'bg-rose-300/10',
        text: 'text-rose-300',
        glow: 'rgba(248, 113, 113, 0.25)',
        border: 'border-rose-300/30',
      };
    }
    switch (currentState) {
      case 'live':
        return {
          bg: 'bg-emerald-400/10',
          text: 'text-emerald-300',
          glow: 'rgba(74, 222, 128, 0.22)',
          border: 'border-emerald-300/30',
        };
      case 'pause':
        return {
          bg: 'bg-amber-300/10',
          text: 'text-amber-200',
          glow: 'rgba(251, 191, 36, 0.2)',
          border: 'border-amber-200/30',
        };
      case 'flatten':
        return {
          bg: 'bg-rose-300/10',
          text: 'text-rose-300',
          glow: 'rgba(248, 113, 113, 0.2)',
          border: 'border-rose-300/30',
        };
    }
  };

  const colors = getStateColor();

  return (
    <div className="relative" ref={containerRef} style={{ width: BTN, height: BTN }}>
      {/* Main button — always at the top, fixed size in layout */}
      <motion.div
        className={`relative overflow-hidden rounded-2xl border ${colors.bg} ${colors.border} backdrop-blur-xl`}
        style={{ width: BTN, touchAction: 'none', userSelect: 'none', cursor: 'pointer' }}
        animate={{ height: armed ? BTN + TRACK_H : BTN }}
        transition={{ type: 'spring', stiffness: 400, damping: 30 }}
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerCancel}
        onPointerCancel={onPointerCancel}
        onWheel={onWheel}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
        aria-label="Master control"
      >
        {/* Glow */}
        <motion.div
          className="absolute inset-0 rounded-2xl blur-xl"
          style={{ backgroundColor: colors.glow }}
          animate={{
            opacity:
              armed
                ? [0.2, 0.5, 0.2]
                : isSystemRunning && currentState === 'live'
                  ? [0.22, 0.48, 0.22]
                  : isBusy
                    ? [0.15, 0.35, 0.15]
                    : 0.16,
          }}
          transition={{
            duration: armed ? 0.8 : isBusy ? 1.2 : 2.4,
            repeat:
              armed || (isSystemRunning && currentState === 'live') || isBusy
                ? Infinity
                : 0,
            ease: 'easeInOut',
          }}
        />

        {/* Vertical slide fill (from bottom up when armed) */}
        {armed && (
          <motion.div
            className="absolute inset-x-0 bottom-0 bg-rose-500/20 rounded-b-2xl"
            animate={{ height: `${slideProgress * 100}%` }}
            transition={{ duration: 0.05, ease: 'linear' }}
          />
        )}

        {/* Power icon — always at top of the component */}
        <div
          className={`relative z-10 flex items-center justify-center ${colors.text}`}
          style={{ height: BTN, width: BTN }}
        >
          {isBusy ? (
            <Loader2 size={14} strokeWidth={2.3} className="animate-spin" />
          ) : (
            <Power size={14} strokeWidth={2.3} />
          )}
        </div>

        {/* Scroll / swipe hint when armed */}
        <AnimatePresence>
          {armed && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="absolute bottom-2 left-0 right-0 flex flex-col items-center gap-0.5 text-rose-300/60"
            >
              <svg width="12" height="8" viewBox="0 0 12 8" fill="none">
                <path
                  d="M1 1L6 6L11 1"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
              <span className="text-[7px] font-medium uppercase tracking-widest">
                stop
              </span>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>

      {/* Hint tooltip */}
      <AnimatePresence>
        {hintText && !armed && (
          <motion.div
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            className="absolute right-0 whitespace-nowrap rounded-lg border border-white/10 bg-black/55 px-2.5 py-1 text-[11px] text-gray-300 backdrop-blur-xl"
            style={{ top: BTN + 8 }}
          >
            {hintText}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
