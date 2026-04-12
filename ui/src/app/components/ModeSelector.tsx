import { motion } from 'motion/react';
import { Shield, Scale, Zap } from 'lucide-react';
import { useState } from 'react';
import { api } from '../lib/api';

type Mode = 'defender' | 'trader' | 'hunter';

interface ModeSelectorProps {
  selectedMode: Mode;
  onModeChange: (mode: Mode) => void;
  onHaptic?: () => void;
  /** When true (e.g. control flattened / system not live-trading), no mode shows the active glow — all icons look idle. */
  inactiveVisual?: boolean;
  /** When true, mode cannot be changed (system fully off). */
  disabled?: boolean;
}

const MODE_META: Record<Mode, { icon: typeof Shield; label: string; color: string }> = {
  hunter:   { icon: Zap,    label: 'Hunter',   color: 'text-amber-300' },
  trader:   { icon: Scale,  label: 'Trader',   color: 'text-white' },
  defender: { icon: Shield, label: 'Defender', color: 'text-sky-300' },
};

export function ModeSelector({
  selectedMode,
  onModeChange,
  onHaptic,
  inactiveVisual = false,
  disabled = false,
}: ModeSelectorProps) {
  const [pendingMode, setPendingMode] = useState<Mode | null>(null);

  const handleModeClick = async (mode: Mode) => {
    if (disabled) return;
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
    <div
      className={`flex flex-col gap-8 ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      {modes.map((id) => {
        const { icon: Icon, label, color } = MODE_META[id];
        const isSelected = selectedMode === id;
        const showActive = isSelected && !inactiveVisual;
        const isPending = pendingMode === id;
        return (
          <div key={id} className="relative">
            <button
              type="button"
              onClick={() => handleModeClick(id)}
              className="relative group"
              aria-label={label}
              aria-disabled={disabled}
              disabled={disabled || isPending}
            >
              <motion.div
                className="relative z-10"
                animate={{ scale: showActive ? 1.1 : 1, opacity: isPending ? 0.5 : 1 }}
                whileHover={disabled ? undefined : { scale: inactiveVisual ? 1.05 : 1.15 }}
                whileTap={disabled ? undefined : { scale: 0.95 }}
              >
                <Icon
                  size={28}
                  strokeWidth={1.5}
                  className={`transition-colors ${
                    showActive
                      ? `${color} drop-shadow-[0_0_8px_rgba(255,255,255,0.8)]`
                      : 'text-gray-500'
                  }`}
                />
              </motion.div>

              {showActive && (
                <motion.div
                  className="absolute inset-0 -m-2 rounded-full bg-white/10 blur-xl"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ duration: 0.3 }}
                />
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}
