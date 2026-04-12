import { motion } from 'motion/react';
import { TrendingUp, TrendingDown } from 'lucide-react';

interface Position {
  symbol: string;
  change: number;
}

interface PositionChipsProps {
  positions: Position[];
  isFlattened?: boolean;
  onHaptic?: () => void;
}

export function PositionChips({ positions, isFlattened = false, onHaptic }: PositionChipsProps) {
  if (positions.length === 0) {
    return (
      <div className="text-center text-xs font-light text-gray-600">
        No open positions
      </div>
    );
  }

  return (
    <div className="flex w-full min-w-0 flex-wrap justify-start gap-2">
      {positions.map((position) => {
        const isPositive = position.change >= 0;
        const chipClass = isFlattened
          ? 'bg-slate-400/10 text-slate-400'
          : isPositive
            ? 'bg-green-500/10 text-green-400'
            : 'bg-red-500/10 text-red-400';
        return (
          <motion.button
            key={position.symbol}
            className={`px-4 py-2 rounded-full text-sm font-light flex items-center gap-2 transition-all ${chipClass}`}
            whileHover={{
              scale: 1.05,
              backgroundColor: isFlattened
                ? 'rgba(148, 163, 184, 0.14)'
                : isPositive
                  ? 'rgba(34, 197, 94, 0.15)'
                  : 'rgba(239, 68, 68, 0.15)',
            }}
            whileTap={{ scale: 0.95 }}
            onClick={() => onHaptic?.()}
          >
            <span className="font-medium">{position.symbol}</span>
            {isPositive ? (
              <TrendingUp size={14} strokeWidth={2} />
            ) : (
              <TrendingDown size={14} strokeWidth={2} />
            )}
            <span>
              {isPositive ? '+' : ''}{position.change.toFixed(2)}%
            </span>
          </motion.button>
        );
      })}
    </div>
  );
}
