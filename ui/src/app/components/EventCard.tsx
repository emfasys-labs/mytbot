import { motion } from 'motion/react';
import { X } from 'lucide-react';

interface EventCardProps {
  label: string;
  onClose: () => void;
}

export function EventCard({ label, onClose }: EventCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.9, y: 20 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.9, y: 20 }}
      transition={{ type: 'spring', stiffness: 300, damping: 25 }}
      className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-50"
    >
      <div className="bg-zinc-900/95 backdrop-blur-xl rounded-2xl p-6 shadow-2xl border border-white/10 min-w-[280px]">
        <div className="flex items-start justify-between mb-2">
          <div className="text-white font-light text-lg">{label}</div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-white transition-colors"
          >
            <X size={18} strokeWidth={1.5} />
          </button>
        </div>
        <div className="text-gray-400 text-sm font-light">
          System responding to market conditions
        </div>
      </div>
    </motion.div>
  );
}
