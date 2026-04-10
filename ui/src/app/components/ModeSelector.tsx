import { motion, AnimatePresence } from 'motion/react';
import { Shield, Scale, Zap } from 'lucide-react';
import { useState } from 'react';
import { api } from '../lib/api';

type Mode = 'defender' | 'trader' | 'hunter';

interface ModeSelectorProps {
  selectedMode: Mode;
  onModeChange: (mode: Mode) => void;
  onHaptic?: () => void;
}

const MODE_META: Record<Mode, { icon: typeof Shield; label: string; description: string; color: string }> = {
  hunter:   { icon: Zap,    label: 'Hunter',   description: 'Aggressive — larger positions, lower confidence bar, more trades', color: 'text-amber-300' },
  trader:   { icon: Scale,  label: 'Trader',   description: 'Balanced — standard risk parameters', color: 'text-white' },
  defender: { icon: Shield, label: 'Defender', description: 'Conservative — smaller positions, higher confidence required, fewer trades', color: 'text-sky-300' },
};

export function ModeSelector({ selectedMode, onModeChange, onHaptic }: ModeSelectorProps) {
  const [tooltip, setTooltip] = useState<Mode | null>(null);
  const [pendingMode, setPendingMode] = useState<Mode | null>(null);

  const handleModeClick = async (mode: Mode) => {
    if (mode === selectedMode) return;
    onHaptic?.();
    setPendingMode(mode);
    try {
      await api.setSystemMode(mode);
      onModeChange(mode);
    } catch {
      // best-effort — UI state still changes for responsiveness
      onModeChange(mode);
    } finally {
      setPendingMode(null);
    }
  };

  const modes = (['hunter', 'trader', 'defender'] as Mode[]);

  return (
    <div className="flex flex-col gap-8">
      {modes.map((id) => {
        const { icon: Icon, label, description, color } = MODE_META[id];
        const isSelected = selectedMode === id;
        const isPending = pendingMode === id;
        const isHovered = tooltip === id;
        return (
          <div key={id} className="relative">
            <button
              onClick={() => handleModeClick(id)}
              onMouseEnter={() => setTooltip(id)}
              onMouseLeave={() => setTooltip(null)}
              className="relative group"
              aria-label={label}
              disabled={isPending}
            >
              <motion.div
                className="relative z-10"
                animate={{ scale: isSelected ? 1.1 : 1, opacity: isPending ? 0.5 : 1 }}
                whileHover={{ scale: 1.15 }}
                whileTap={{ scale: 0.95 }}
              >
                <Icon
                  size={28}
                  strokeWidth={1.5}
                  className={`transition-colors ${
                    isSelected
                      ? `${color} drop-shadow-[0_0_8px_rgba(255,255,255,0.8)]`
                      : 'text-gray-500'
                  }`}
                />
              </motion.div>

              {isSelected && (
                <motion.div
                  className="absolute inset-0 -m-2 rounded-full bg-white/10 blur-xl"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ duration: 0.3 }}
                />
              )}
            </button>

            {/* Tooltip on hover — shows label + description */}
            <AnimatePresence>
              {(isHovered || isPending) && (
                <motion.div
                  initial={{ opacity: 0, x: 10 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: 10 }}
                  transition={{ duration: 0.15 }}
                  className="absolute left-12 top-1/2 -translate-y-1/2 z-50 pointer-events-none"
                >
                  <div className="bg-[#111]/90 backdrop-blur-xl border border-white/10 px-3 py-2 rounded-xl shadow-xl max-w-[200px]">
                    <div className={`text-sm font-medium ${color}`}>{label}</div>
                    <div className="text-[10px] text-gray-400 mt-0.5 leading-relaxed">{description}</div>
                    {isPending && (
                      <div className="text-[10px] text-amber-400 mt-1">Applying…</div>
                    )}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        );
      })}
    </div>
  );
}
