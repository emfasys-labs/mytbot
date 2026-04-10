import { motion } from 'motion/react';

interface NewsTickerProps {
  isFlattened?: boolean;
  items?: string[];
}

export function NewsTicker({ isFlattened = false, items }: NewsTickerProps) {
  const hasItems = Boolean(items && items.length > 0);
  const duplicatedNews = hasItems ? [...(items as string[]), ...(items as string[])] : [];

  return (
    <div className="w-full overflow-hidden bg-white/[0.02] border-b border-white/[0.05] py-3">
      {isFlattened ? (
        <div className="text-center text-sm font-light text-gray-600">News paused</div>
      ) : !hasItems ? (
        <div className="text-center text-sm font-light text-gray-600">No live news context yet</div>
      ) : (
        <motion.div
          className="flex gap-12 whitespace-nowrap"
          animate={{
            x: [0, -50 + '%'],
          }}
          transition={{
            duration: 40,
            repeat: Infinity,
            ease: 'linear',
          }}
        >
          {duplicatedNews.map((item, index) => (
            <span key={index} className="text-sm font-light text-gray-500">
              {item}
            </span>
          ))}
        </motion.div>
      )}
    </div>
  );
}
