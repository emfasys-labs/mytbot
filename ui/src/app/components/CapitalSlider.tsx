import { motion, AnimatePresence } from 'motion/react';
import { useRef, useState } from 'react';
import { api } from '../lib/api';

interface CapitalSliderProps {
  totalCapital: number;
  pct: number;
  onPctChange: (pct: number) => void;
  onHaptic?: () => void;
  dormant?: boolean;
}

export function CapitalSlider({ totalCapital, pct, onPctChange, onHaptic, dormant = false }: CapitalSliderProps) {
  const [isDragging, setIsDragging] = useState(false);
  const displayPct = dormant ? 0 : Math.max(0, Math.min(100, pct * 100));
  const activeCapital = totalCapital * pct;
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleSliderChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (dormant) return;
    const value = parseFloat(e.target.value);
    const newPct = Math.max(0, Math.min(1, value / 100));
    onPctChange(newPct);

    if (Math.abs(value % 25) < 1) {
      onHaptic?.();
    }
  };

  const commitToBackend = (newPct: number) => {
    if (commitTimer.current) clearTimeout(commitTimer.current);
    commitTimer.current = setTimeout(() => {
      api.setCapitalAllocation(newPct).catch(() => {});
    }, 400);
  };

  const handleDragEnd = () => {
    setIsDragging(false);
    commitToBackend(pct);
  };

  return (
    <div className="relative flex h-96 flex-col items-center">
      <div className="relative h-full w-12 flex flex-col items-center">
      <AnimatePresence>
        {isDragging && !dormant && (
          <motion.div
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -10 }}
            transition={{ duration: 0.2 }}
            className="absolute -left-40 top-1/2 -translate-y-1/2 bg-white/10 backdrop-blur-xl px-4 py-2 rounded-lg"
          >
            <div className="text-xs text-gray-400 mb-0.5">Tradable</div>
            <div className="text-xl font-medium text-amber-400">
              ${Math.round(activeCapital).toLocaleString()}
            </div>
            <div className="text-xs text-gray-500 mt-0.5">
              {Math.round(pct * 100)}% of capital
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="relative w-2 h-full bg-white/5 rounded-full overflow-hidden">
        <motion.div
          className="absolute bottom-0 left-0 right-0 rounded-full"
          animate={{ height: `${displayPct}%` }}
          style={{
            background: 'linear-gradient(to top, #4ade80, #fbbf24)',
          }}
          transition={{ type: 'spring', stiffness: 200, damping: 25 }}
        />

        <input
          type="range"
          min="0"
          max="100"
          step="1"
          value={dormant ? 0 : Math.round(pct * 100)}
          disabled={dormant || totalCapital <= 0}
          onChange={handleSliderChange}
          onMouseDown={() => {
            if (dormant) return;
            setIsDragging(true);
            onHaptic?.();
          }}
          onMouseUp={handleDragEnd}
          onTouchStart={() => {
            if (dormant) return;
            setIsDragging(true);
            onHaptic?.();
          }}
          onTouchEnd={handleDragEnd}
          className={`absolute inset-0 h-full w-full opacity-0 ${!dormant && totalCapital > 0 ? 'cursor-pointer' : 'cursor-not-allowed'}`}
          style={{
            WebkitAppearance: 'slider-vertical',
            writingMode: 'vertical-lr',
            direction: 'rtl',
          }}
        />
      </div>

      <motion.div
        className="absolute w-6 h-6 rounded-full bg-white shadow-lg pointer-events-none"
        animate={{
          bottom: `calc(${displayPct}% - 12px)`,
          scale: isDragging ? 1.3 : 1,
          opacity: dormant ? 0.3 : 1,
        }}
        style={{
          boxShadow: '0 0 20px rgba(255, 255, 255, 0.5)',
        }}
        transition={{ type: 'spring', stiffness: 200, damping: 25 }}
      />
      </div>
    </div>
  );
}
