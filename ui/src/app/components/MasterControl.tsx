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

const BTN_SIZE = 44;
const EXPANDED_H = 140;
const HOLD_MS = 800;

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
  const dragStartY = useRef(0);
  const containerRef = useRef<HTMLDivElement>(null);

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
  };

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (isBusy) return;

    if (armed) {
      e.currentTarget.setPointerCapture(e.pointerId);
      dragStartY.current = e.clientY;
      return;
    }

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
      }
    }, HOLD_MS);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!armed) return;
    const dy = e.clientY - dragStartY.current;
    const trackH = EXPANDED_H - BTN_SIZE;
    const pct = Math.max(0, Math.min(1, dy / trackH));
    setSlideProgress(pct);
  };

  const onPointerUp = () => {
    clearLongPress();

    if (armed) {
      if (slideProgress >= 0.9) {
        doStop();
      } else {
        disarm();
      }
      pointerDown.current = false;
      return;
    }

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
    disarm();
    pointerDown.current = false;
  };

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
  const height = armed ? EXPANDED_H : BTN_SIZE;

  return (
    <div className="relative" ref={containerRef}>
      <motion.div
        className={`relative overflow-hidden rounded-2xl border ${colors.bg} ${colors.border} backdrop-blur-xl`}
        animate={{ height, width: BTN_SIZE }}
        transition={{ type: 'spring', stiffness: 400, damping: 30 }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerCancel}
        onPointerCancel={onPointerCancel}
        style={{ touchAction: 'none', userSelect: 'none', cursor: 'pointer' }}
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
            repeat: armed || (isSystemRunning && currentState === 'live') || isBusy ? Infinity : 0,
            ease: 'easeInOut',
          }}
        />

        {/* Vertical slide fill (bottom-up rose fill when armed) */}
        {armed && (
          <motion.div
            className="absolute inset-x-0 bottom-0 bg-rose-500/20 rounded-b-2xl"
            animate={{ height: `${slideProgress * 100}%` }}
            transition={{ duration: 0.05, ease: 'linear' }}
          />
        )}

        {/* Power icon at top */}
        <div
          className={`relative z-10 flex items-center justify-center ${colors.text}`}
          style={{ height: BTN_SIZE, width: BTN_SIZE }}
        >
          {isBusy ? (
            <Loader2 size={14} strokeWidth={2.3} className="animate-spin" />
          ) : (
            <Power size={14} strokeWidth={2.3} />
          )}
        </div>

        {/* Arrow indicator when armed */}
        <AnimatePresence>
          {armed && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="absolute bottom-2 left-0 right-0 flex flex-col items-center gap-0.5 text-rose-300/60"
            >
              <svg width="12" height="8" viewBox="0 0 12 8" fill="none">
                <path d="M1 1L6 6L11 1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
              <span className="text-[7px] font-medium uppercase tracking-widest">stop</span>
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
            className="absolute right-0 top-14 whitespace-nowrap rounded-lg border border-white/10 bg-black/55 px-2.5 py-1 text-[11px] text-gray-300 backdrop-blur-xl"
          >
            {hintText}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
