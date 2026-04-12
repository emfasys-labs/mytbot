import { motion } from 'motion/react';
import { useEffect, useState } from 'react';

type TrendState = 'positive' | 'mixed' | 'drawdown';

interface EquityLineProps {
  balance: number;
  dailyPnL: number;
  state: TrendState;
  isActive: boolean;
  isFlattened?: boolean;
  historyValues?: number[];
  /** Shorter viewBox so the chart fits embedded panels without overflowing siblings. */
  compact?: boolean;
  /** Indices into the rendered series for filled-trade markers (green = positive, red = loss). */
  tradeMarkers?: Array<{ index: number; positive: boolean }>;
}

export function EquityLine({
  balance,
  dailyPnL,
  state,
  isActive,
  isFlattened = false,
  historyValues,
  compact = false,
  tradeMarkers,
}: EquityLineProps) {
  const [dataPoints, setDataPoints] = useState<number[]>([]);

  useEffect(() => {
    if (historyValues && historyValues.length > 1) {
      setDataPoints(historyValues.slice(-80));
      return;
    }

    const points: number[] = [];
    const baseValue = balance - dailyPnL;
    const steps = 50;
    
    for (let i = 0; i < steps; i++) {
      const progress = i / steps;
      const variation = Math.sin(progress * Math.PI * 3) * (dailyPnL * 0.1);
      const value = baseValue + (dailyPnL * progress) + variation;
      points.push(value);
    }
    
    setDataPoints(points);
  }, [balance, dailyPnL, historyValues]);

  // Animate the line gradually

  const getColorScheme = () => {
    if (isFlattened) {
      return {
        stroke: 'rgba(168, 176, 188, 0.72)',
        fill: 'rgba(128, 138, 152, 0.12)',
        glow: 'rgba(163, 171, 182, 0.22)',
      };
    }
    switch (state) {
      case 'positive':
        return {
          stroke: 'rgba(74, 222, 128, 0.6)',
          fill: 'rgba(74, 222, 128, 0.1)',
          glow: 'rgba(74, 222, 128, 0.4)',
        };
      case 'mixed':
        return {
          stroke: 'rgba(251, 191, 36, 0.6)',
          fill: 'rgba(251, 191, 36, 0.1)',
          glow: 'rgba(251, 191, 36, 0.4)',
        };
      case 'drawdown':
        return {
          stroke: 'rgba(248, 113, 113, 0.6)',
          fill: 'rgba(248, 113, 113, 0.1)',
          glow: 'rgba(248, 113, 113, 0.4)',
        };
    }
  };

  const colors = getColorScheme();
  const shouldPulse = isActive && !isFlattened;

  // Generate SVG path (compact = short chart for dashboard panels — must match wrapper max-h)
  const width = 800;
  const height = compact ? 120 : 300;
  const padding = compact ? 16 : 40;
  
  if (dataPoints.length === 0) return null;

  const minValue = Math.min(...dataPoints);
  const maxValue = Math.max(...dataPoints);
  const range = maxValue - minValue || 1;

  const pointPairs = dataPoints.map((value, index) => {
    const x = padding + (index / (dataPoints.length - 1)) * (width - padding * 2);
    const y = height - padding - ((value - minValue) / range) * (height - padding * 2);
    return { x, y };
  });

  const points = pointPairs.map((p) => `${p.x},${p.y}`);

  const pathD = `M ${points.join(' L ')}`;
  const areaD = `${pathD} L ${width - padding},${height - padding} L ${padding},${height - padding} Z`;

  const lastPt = pointPairs[pointPairs.length - 1];
  const currentX = lastPt?.x ?? padding + (width - padding * 2);
  const currentY = lastPt?.y ?? height - padding;

  return (
    <div className="relative w-full h-full min-h-0 flex items-center justify-center overflow-hidden">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="max-h-full w-full max-w-full"
        preserveAspectRatio="xMidYMid meet"
      >
        {/* Area fill */}
        <motion.path
          d={areaD}
          fill={colors.fill}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 1 }}
        />

        {/* Main line */}
        <motion.path
          d={pathD}
          fill="none"
          stroke={colors.stroke}
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 2, ease: 'easeOut' }}
        />

        {tradeMarkers?.map((m, i) => {
          const pt = pointPairs[m.index];
          if (!pt) return null;
          const fill = m.positive ? 'rgba(74, 222, 128, 0.95)' : 'rgba(248, 113, 113, 0.95)';
          return (
            <circle key={`tm-${m.index}-${i}`} cx={pt.x} cy={pt.y} r={3} fill={fill} opacity={0.9} />
          );
        })}

        {/* Current position dot */}
        <motion.circle
          cx={currentX}
          cy={currentY}
          r="4"
          fill={colors.stroke}
          animate={{
            scale: shouldPulse ? [1, 1.3, 1] : 1,
            opacity: shouldPulse ? [1, 0.7, 1] : 0.8,
          }}
          transition={{
            duration: 2,
            repeat: shouldPulse ? Infinity : 0,
            ease: 'easeInOut',
          }}
        />

        {/* Glow effect on dot */}
        <motion.circle
          cx={currentX}
          cy={currentY}
          r="8"
          fill={colors.glow}
          opacity="0.3"
          animate={{
            scale: shouldPulse ? [1, 1.5, 1] : 1,
            opacity: shouldPulse ? [0.3, 0, 0.3] : 0,
          }}
          transition={{
            duration: 2,
            repeat: shouldPulse ? Infinity : 0,
            ease: 'easeInOut',
          }}
        />
      </svg>
    </div>
  );
}
