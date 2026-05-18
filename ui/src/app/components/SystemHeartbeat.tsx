import { motion } from 'motion/react';

type ControlState = 'live' | 'pause' | 'flatten';

interface SystemHeartbeatProps {
  isActive: boolean;
  controlState: ControlState;
  tradesCount: number;
  lastTradeMinutes: number;
}

export function SystemHeartbeat({
  isActive,
  controlState,
  tradesCount,
  lastTradeMinutes,
}: SystemHeartbeatProps) {
  const dotClass =
    controlState === 'live'
      ? 'bg-emerald-400'
      : controlState === 'pause'
        ? 'bg-amber-300'
        : 'bg-rose-400';
  const statusLabel =
    controlState === 'live' ? 'Active' : controlState === 'pause' ? 'Paused' : 'Flattened';

  return (
    <div className="flex items-center gap-2 text-sm font-light text-gray-500">
      {/* Status indicator */}
      <motion.div
        className={`h-2 w-2 rounded-full ${dotClass}`}
        animate={{
          opacity: isActive ? [1, 0.5, 1] : controlState === 'flatten' ? 0.95 : 0.75,
          scale: isActive ? [1, 1.2, 1] : 1,
        }}
        transition={{
          duration: 2,
          repeat: isActive ? Infinity : 0,
          ease: 'easeInOut',
        }}
      />
      <span>{statusLabel}</span>
      <span>·</span>
      <span>{tradesCount} fills today</span>
      <span>·</span>
      <span>last trade {lastTradeMinutes}m ago</span>
    </div>
  );
}
