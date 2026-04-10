import { motion, AnimatePresence } from 'motion/react';
import { Shield, Scale, Zap } from 'lucide-react';
import { useState } from 'react';

type Mode = 'defender' | 'trader' | 'hunter';

interface ModeSelectorProps {
  selectedMode: Mode;
  onModeChange: (mode: Mode) => void;
  onHaptic?: () => void;
}

export function ModeSelector({ selectedMode, onModeChange, onHaptic }: ModeSelectorProps) {
  const [showLabel, setShowLabel] = useState<Mode | null>(null);

  const modes: { id: Mode; icon: typeof Shield; label: string }[] = [
    { id: 'hunter', icon: Zap, label: 'Hunter' },
    { id: 'trader', icon: Scale, label: 'Trader' },
    { id: 'defender', icon: Shield, label: 'Defender' },
  ];

  const handleModeClick = (mode: Mode) => {
    onModeChange(mode);
    onHaptic?.();
    setShowLabel(mode);
    setTimeout(() => setShowLabel(null), 1500);
  };

  return (
    <div className="flex flex-col gap-8">
      {modes.map(({ id, icon: Icon, label }) => {
        const isSelected = selectedMode === id;
        return (
          <div key={id} className="relative">
            <button
              onClick={() => handleModeClick(id)}
              className="relative group"
              aria-label={label}
            >
              <motion.div
                className="relative z-10"
                animate={{
                  scale: isSelected ? 1.1 : 1,
                }}
                whileHover={{ scale: 1.15 }}
                whileTap={{ scale: 0.95 }}
              >
                <Icon
                  size={28}
                  strokeWidth={1.5}
                  className={`transition-colors ${
                    isSelected
                      ? 'text-white drop-shadow-[0_0_8px_rgba(255,255,255,0.8)]'
                      : 'text-gray-500'
                  }`}
                />
              </motion.div>

              {/* Selection glow */}
              {isSelected && (
                <motion.div
                  className="absolute inset-0 -m-2 rounded-full bg-white/10 blur-xl"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ duration: 0.3 }}
                />
              )}
            </button>

            {/* Label on tap */}
            <AnimatePresence>
              {showLabel === id && (
                <motion.div
                  initial={{ opacity: 0, x: 10 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: 10 }}
                  transition={{ duration: 0.2 }}
                  className="absolute left-12 top-1/2 -translate-y-1/2 bg-white/10 backdrop-blur-xl px-3 py-1 rounded-lg text-sm font-light whitespace-nowrap"
                >
                  {label}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        );
      })}
    </div>
  );
}