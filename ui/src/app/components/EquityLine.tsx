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
}

export function EquityLine({
  balance,
  dailyPnL,
  state,
  isActive,
  isFlattened = false,
  historyValues,
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

  // Generate SVG path
  const width = 800;
  const height = 300;
  const padding = 40;
  
  if (dataPoints.length === 0) return null;

  const minValue = Math.min(...dataPoints);
  const maxValue = Math.max(...dataPoints);
  const range = maxValue - minValue || 1;

  const points = dataPoints.map((value, index) => {
    const x = padding + (index / (dataPoints.length - 1)) * (width - padding * 2);
    const y = height - padding - ((value - minValue) / range) * (height - padding * 2);
    return `${x},${y}`;
  });

  const pathD = `M ${points.join(' L ')}`;
  const areaD = `${pathD} L ${width - padding},${height - padding} L ${padding},${height - padding} Z`;

  const currentX = padding + (width - padding * 2);
  const currentY = height - padding - ((dataPoints[dataPoints.length - 1] - minValue) / range) * (height - padding * 2);

  return (
    <div className="relative w-full flex items-center justify-center">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="max-w-full"
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
