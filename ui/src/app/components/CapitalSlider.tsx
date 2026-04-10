import { motion, AnimatePresence } from 'motion/react';
import { useState } from 'react';

interface CapitalSliderProps {
  totalCapital: number;
  activeCapital: number;
  onCapitalChange: (active: number) => void;
  onHaptic?: () => void;
}

export function CapitalSlider({ totalCapital, activeCapital, onCapitalChange, onHaptic }: CapitalSliderProps) {
  const [isDragging, setIsDragging] = useState(false);
  const safeTotal = Math.max(0, totalCapital);
  const percentage = safeTotal > 0 ? Math.max(0, Math.min(100, (activeCapital / safeTotal) * 100)) : 0;

  const handleSliderChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = parseFloat(e.target.value);
    onCapitalChange((value / 100) * safeTotal);
    
    // Trigger haptic at 25% intervals
    if (Math.abs(value % 25) < 1) {
      onHaptic?.();
    }
  };

  return (
    <div className="relative h-96 w-12 flex flex-col items-center">
      {/* Exposed capital label - shows when dragging */}
      <AnimatePresence>
        {isDragging && (
          <motion.div
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -10 }}
            transition={{ duration: 0.2 }}
            className="absolute -left-40 top-1/2 -translate-y-1/2 bg-white/10 backdrop-blur-xl px-4 py-2 rounded-lg"
          >
            <div className="text-xs text-gray-400 mb-0.5">Exposing</div>
            <div className="text-xl font-medium text-amber-400">
              £{activeCapital.toLocaleString()}
            </div>
            <div className="text-xs text-gray-500 mt-0.5">
              {percentage.toFixed(0)}% of capital
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Slider track */}
      <div className="relative w-2 h-full bg-white/5 rounded-full overflow-hidden">
        {/* Gradient fill */}
        <motion.div
          className="absolute bottom-0 left-0 right-0 rounded-full"
          style={{
            height: `${percentage}%`,
            background: 'linear-gradient(to top, #4ade80, #fbbf24)',
          }}
          transition={{ type: 'spring', stiffness: 300, damping: 30 }}
        />

        {/* Input range */}
        <input
          type="range"
          min="0"
          max="100"
          step="1"
          value={percentage}
          disabled={safeTotal <= 0}
          onChange={handleSliderChange}
          onMouseDown={() => {
            setIsDragging(true);
            onHaptic?.();
          }}
          onMouseUp={() => setIsDragging(false)}
          onTouchStart={() => {
            setIsDragging(true);
            onHaptic?.();
          }}
          onTouchEnd={() => setIsDragging(false)}
          className={`absolute inset-0 h-full w-full opacity-0 ${safeTotal > 0 ? 'cursor-pointer' : 'cursor-not-allowed'}`}
          style={{
            WebkitAppearance: 'slider-vertical',
            writingMode: 'vertical-lr',
            direction: 'rtl',
          }}
        />
      </div>

      {/* Slider thumb */}
      <motion.div
        className="absolute w-6 h-6 rounded-full bg-white shadow-lg pointer-events-none"
        style={{
          bottom: `calc(${percentage}% - 12px)`,
          boxShadow: '0 0 20px rgba(255, 255, 255, 0.5)',
        }}
        animate={{
          scale: isDragging ? 1.3 : 1,
        }}
        transition={{ type: 'spring', stiffness: 300, damping: 20 }}
      />
    </div>
  );
}