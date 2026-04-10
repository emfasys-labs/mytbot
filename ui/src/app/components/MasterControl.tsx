import { AnimatePresence, motion } from 'motion/react';
import { Loader2, Power, TriangleAlert } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
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

export function MasterControl({
  currentState,
  systemState,
  onStateChange,
  onSystemStart,
  onSystemStop,
  onHaptic,
}: MasterControlProps) {
  const HOLD_MS = 1500;
  const WHEEL_STEP = 0.12;

  const [isHolding, setIsHolding] = useState(false);
  const [showSlideConfirm, setShowSlideConfirm] = useState(false);
  const [slideProgress, setSlideProgress] = useState(0);
  const [hintText, setHintText] = useState<string | null>(null);
  const [isTransitioning, setIsTransitioning] = useState(false);

  const longPressTimer = useRef<NodeJS.Timeout | null>(null);
  const hintTimer = useRef<NodeJS.Timeout | null>(null);
  const pointerDown = useRef(false);
  const longPressTriggered = useRef(false);
  const confirmDragging = useRef(false);

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

  const handleStateSelect = (state: ControlState, haptic = true) => {
    onStateChange(state);
    if (haptic) onHaptic?.();
  };

  const clearLongPress = () => {
    if (longPressTimer.current) clearTimeout(longPressTimer.current);
    longPressTimer.current = null;
  };

  const pushSlideProgress = (next: number) => {
    const clamped = Math.max(0, Math.min(1, next));
    setSlideProgress(clamped);
    if (clamped >= 1) {
      handleSystemStop();
      setShowSlideConfirm(false);
      setSlideProgress(0);
      setIsHolding(false);
      pointerDown.current = false;
      clearLongPress();
      confirmDragging.current = false;
    }
  };

  const handleSystemStart = async () => {
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

  const handleSystemStop = async () => {
    if (isBusy) return;
    setIsTransitioning(true);
    flashHint('Stopping...');
    try {
      await onSystemStop();
      handleStateSelect('flatten', false);
      flashHint('System OFF');
    } catch {
      flashHint('Stop failed');
    } finally {
      setIsTransitioning(false);
    }
  };

  const resetInteraction = () => {
    pointerDown.current = false;
    setIsHolding(false);
    if (!showSlideConfirm) {
      setSlideProgress(0);
    }
  };

  const onButtonPointerDown = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (isBusy) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    pointerDown.current = true;
    longPressTriggered.current = false;
    setIsHolding(true);

    clearLongPress();
    longPressTimer.current = setTimeout(() => {
      if (!pointerDown.current) return;
      longPressTriggered.current = true;
      onHaptic?.();

      if (isSystemOff) {
        handleSystemStart();
        setIsHolding(false);
      } else if (isSystemRunning) {
        setShowSlideConfirm(true);
        setSlideProgress(0);
        flashHint('Slide to stop system');
      }
    }, HOLD_MS);
  };

  const onButtonPointerMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!pointerDown.current) return;
    if (!showSlideConfirm) return;
    e.preventDefault();
  };

  const onButtonPointerUp = () => {
    clearLongPress();

    if (!longPressTriggered.current) {
      if (isSystemOff) {
        handleSystemStart();
      } else if (isSystemRunning) {
        if (currentState === 'live') {
          handleStateSelect('pause');
          flashHint('Paused');
        } else if (currentState === 'pause') {
          handleStateSelect('live');
          flashHint('Live');
        } else if (currentState === 'flatten') {
          handleStateSelect('live');
          flashHint('Live');
        }
      } else if (isBusy) {
        flashHint(isSystemStarting ? 'Starting...' : 'Stopping...');
      }
    }

    if (showSlideConfirm && slideProgress < 1) {
      setShowSlideConfirm(false);
      setSlideProgress(0);
    }
    resetInteraction();
  };

  const onButtonPointerCancel = () => {
    clearLongPress();
    if (showSlideConfirm) {
      setShowSlideConfirm(false);
      setSlideProgress(0);
    }
    resetInteraction();
  };

  const onSlideWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (!showSlideConfirm) return;
    e.preventDefault();
    const delta = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? Math.abs(e.deltaY) : Math.abs(e.deltaX);
    const factor = Math.min(1, delta / 120);
    pushSlideProgress(slideProgress + WHEEL_STEP * factor);
  };

  const onConfirmPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    confirmDragging.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = (e.clientX - rect.left) / rect.width;
    pushSlideProgress(pct);
  };

  const onConfirmPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!confirmDragging.current) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = (e.clientX - rect.left) / rect.width;
    pushSlideProgress(pct);
  };

  const onConfirmPointerUp = () => {
    confirmDragging.current = false;
  };

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
    <div className="relative">
      <motion.button
        className={`relative h-11 w-11 overflow-hidden rounded-2xl border ${colors.bg} ${colors.text} ${colors.border} backdrop-blur-xl`}
        onPointerDown={onButtonPointerDown}
        onPointerMove={onButtonPointerMove}
        onPointerUp={onButtonPointerUp}
        onPointerLeave={onButtonPointerCancel}
        onPointerCancel={onButtonPointerCancel}
        whileTap={{ scale: 0.95 }}
        style={{ touchAction: 'none', userSelect: 'none' }}
        aria-label="Master control"
      >
        <motion.div
          className="absolute inset-0 rounded-2xl blur-xl"
          style={{ backgroundColor: colors.glow }}
          animate={{
            opacity: isSystemRunning && currentState === 'live'
              ? [0.22, 0.48, 0.22]
              : isBusy
                ? [0.15, 0.35, 0.15]
                : 0.16,
          }}
          transition={{
            duration: isBusy ? 1.2 : 2.4,
            repeat: (isSystemRunning && currentState === 'live') || isBusy ? Infinity : 0,
            ease: 'easeInOut',
          }}
        />

        <div className="relative z-10 flex h-full items-center justify-center">
          {isBusy ? (
            <Loader2 size={14} strokeWidth={2.3} className="animate-spin" />
          ) : (
            <Power size={14} strokeWidth={2.3} />
          )}
        </div>
      </motion.button>

      <AnimatePresence>
        {showSlideConfirm && (
          <motion.div
            initial={{ opacity: 0, x: 8 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 8 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            className="absolute right-14 top-1/2 z-30 h-8 w-44 -translate-y-1/2 rounded-full border border-rose-300/20 bg-black/55 px-1 backdrop-blur-xl"
            onWheel={onSlideWheel}
            onPointerDown={onConfirmPointerDown}
            onPointerMove={onConfirmPointerMove}
            onPointerUp={onConfirmPointerUp}
            onPointerCancel={onConfirmPointerUp}
            onPointerLeave={onConfirmPointerUp}
          >
            <div className="relative h-full w-full overflow-hidden rounded-full bg-white/5">
              <motion.div
                className="absolute inset-y-0 left-0 rounded-full bg-rose-400/25"
                animate={{ width: `${slideProgress * 100}%` }}
                transition={{ duration: 0.07, ease: 'linear' }}
              />
              <div className="relative z-10 flex h-full items-center justify-center gap-1 text-[11px] text-rose-200/85">
                <TriangleAlert size={12} />
                <span>Slide to stop</span>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {hintText && (
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

      {isHolding && isSystemOff && !showSlideConfirm && (
        <motion.span
          className="absolute -bottom-6 right-0 text-[11px] text-gray-500"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
        >
          Starting system...
        </motion.span>
      )}

      {isHolding && isSystemRunning && !showSlideConfirm && (
        <motion.span
          className="absolute -bottom-6 right-0 text-[11px] text-gray-500"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
        >
          Hold to arm stop
        </motion.span>
      )}
    </div>
  );
}
