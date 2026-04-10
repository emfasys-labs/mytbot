import { motion, AnimatePresence } from 'motion/react';
import { useEffect, useState } from 'react';

type SystemState = 'stable' | 'active' | 'stress';

interface Event {
  id: string;
  label: string;
  x: number;
  y: number;
}

interface TradingSphereProps {
  state: SystemState;
  events: Event[];
  onEventTap: (event: Event) => void;
  rotation: { x: number; y: number };
  onHaptic?: () => void;
}

export function TradingSphere({ state, events, onEventTap, rotation, onHaptic }: TradingSphereProps) {
  const [showEvents, setShowEvents] = useState(false);
  const [showWorldMap, setShowWorldMap] = useState(false);

  // Show events and world map after rotation
  useEffect(() => {
    const rotationMagnitude = Math.abs(rotation.x) + Math.abs(rotation.y);
    if (rotationMagnitude > 5) {
      setShowEvents(true);
      setShowWorldMap(true);
    }
  }, [rotation]);

  const getStateColor = () => {
    switch (state) {
      case 'stable':
        return { primary: '#4ade80', secondary: '#22c55e', glow: 'rgba(74, 222, 128, 0.4)' };
      case 'active':
        return { primary: '#fbbf24', secondary: '#f59e0b', glow: 'rgba(251, 191, 36, 0.4)' };
      case 'stress':
        return { primary: '#f87171', secondary: '#ef4444', glow: 'rgba(248, 113, 113, 0.4)' };
    }
  };

  const colors = getStateColor();

  // World map coordinates (simplified continents)
  const worldPaths = [
    // North America
    'M 120,80 Q 110,70 100,75 Q 90,80 85,90 Q 80,100 85,110 Q 90,120 100,125 Q 110,130 120,125 Q 130,120 135,110 Q 140,100 135,90 Q 130,80 120,80',
    // Europe
    'M 180,85 Q 175,80 170,82 Q 165,85 162,90 Q 160,95 162,100 Q 165,105 170,107 Q 175,109 180,107 Q 185,105 188,100 Q 190,95 188,90 Q 185,85 180,85',
    // Asia
    'M 220,90 Q 210,85 200,88 Q 190,92 185,100 Q 182,110 188,120 Q 195,128 205,130 Q 215,132 225,128 Q 235,124 240,115 Q 245,105 240,95 Q 235,88 220,90',
    // Africa
    'M 175,120 Q 170,115 165,118 Q 160,122 158,130 Q 157,140 160,150 Q 165,158 172,160 Q 180,162 188,158 Q 195,154 197,145 Q 198,135 195,127 Q 190,120 175,120',
    // South America
    'M 130,140 Q 125,135 120,138 Q 115,142 113,150 Q 112,160 115,170 Q 120,178 127,180 Q 135,182 142,178 Q 148,174 150,165 Q 151,155 148,147 Q 143,140 130,140',
    // Australia
    'M 240,160 Q 235,157 230,159 Q 226,162 225,168 Q 224,174 227,180 Q 231,185 237,186 Q 243,187 248,184 Q 252,181 253,175 Q 254,169 251,163 Q 248,159 240,160',
  ];

  return (
    <div className="relative w-80 h-80 flex items-center justify-center pointer-events-none">
      {/* Glow effect */}
      <motion.div
        className="absolute inset-0 rounded-full blur-3xl pointer-events-none"
        style={{
          background: `radial-gradient(circle, ${colors.glow} 0%, transparent 70%)`,
        }}
        animate={{
          scale: [1, 1.1, 1],
          opacity: [0.6, 0.8, 0.6],
        }}
        transition={{
          duration: 3,
          repeat: Infinity,
          ease: 'easeInOut',
        }}
      />

      {/* Main sphere */}
      <motion.div
        className="relative w-64 h-64 rounded-full overflow-hidden"
        style={{
          background: `radial-gradient(circle at 30% 30%, ${colors.primary}, ${colors.secondary})`,
          boxShadow: `
            inset -20px -20px 40px rgba(0, 0, 0, 0.3),
            inset 10px 10px 30px rgba(255, 255, 255, 0.1),
            0 20px 60px ${colors.glow}
          `,
          transform: `rotateX(${rotation.x}deg) rotateY(${rotation.y}deg)`,
          transformStyle: 'preserve-3d',
        }}
        animate={{
          scale: [1, 1.02, 1],
        }}
        transition={{
          duration: 2.5,
          repeat: Infinity,
          ease: 'easeInOut',
        }}
      >
        {/* World map overlay - SVG continents */}
        <AnimatePresence>
          {showWorldMap && (
            <motion.svg
              className="absolute inset-0 w-full h-full"
              viewBox="0 0 320 320"
              initial={{ opacity: 0 }}
              animate={{ opacity: 0.15 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.8 }}
              style={{
                filter: 'blur(0.5px)',
              }}
            >
              {/* Latitude lines */}
              <motion.line
                x1="0"
                y1="80"
                x2="320"
                y2="80"
                stroke="rgba(255, 255, 255, 0.1)"
                strokeWidth="0.5"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 1, delay: 0.2 }}
              />
              <motion.line
                x1="0"
                y1="160"
                x2="320"
                y2="160"
                stroke="rgba(255, 255, 255, 0.15)"
                strokeWidth="0.5"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 1, delay: 0.3 }}
              />
              <motion.line
                x1="0"
                y1="240"
                x2="320"
                y2="240"
                stroke="rgba(255, 255, 255, 0.1)"
                strokeWidth="0.5"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 1, delay: 0.4 }}
              />

              {/* Longitude lines */}
              <motion.line
                x1="80"
                y1="0"
                x2="80"
                y2="320"
                stroke="rgba(255, 255, 255, 0.1)"
                strokeWidth="0.5"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 1, delay: 0.5 }}
              />
              <motion.line
                x1="160"
                y1="0"
                x2="160"
                y2="320"
                stroke="rgba(255, 255, 255, 0.15)"
                strokeWidth="0.5"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 1, delay: 0.6 }}
              />
              <motion.line
                x1="240"
                y1="0"
                x2="240"
                y2="320"
                stroke="rgba(255, 255, 255, 0.1)"
                strokeWidth="0.5"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 1, delay: 0.7 }}
              />

              {/* Continent outlines */}
              {worldPaths.map((path, index) => (
                <motion.path
                  key={index}
                  d={path}
                  fill="rgba(255, 255, 255, 0.08)"
                  stroke="rgba(255, 255, 255, 0.15)"
                  strokeWidth="0.5"
                  initial={{ opacity: 0, pathLength: 0 }}
                  animate={{ opacity: 1, pathLength: 1 }}
                  transition={{ duration: 1.5, delay: 0.8 + index * 0.1 }}
                />
              ))}
            </motion.svg>
          )}
        </AnimatePresence>

        {/* Subtle surface ripples */}
        <motion.div
          className="absolute inset-0 rounded-full"
          style={{
            background: 'radial-gradient(circle at 50% 50%, rgba(255, 255, 255, 0.15), transparent 40%)',
          }}
          animate={{
            scale: [1, 1.3, 1],
            opacity: [0, 0.5, 0],
          }}
          transition={{
            duration: 4,
            repeat: Infinity,
            ease: 'easeOut',
            repeatDelay: 2,
          }}
        />

        {/* Event markers */}
        {showEvents && events.map((event) => (
          <motion.button
            key={event.id}
            className="absolute w-3 h-3 rounded-full cursor-pointer"
            style={{
              left: `${event.x}%`,
              top: `${event.y}%`,
              background: 'rgba(255, 255, 255, 0.9)',
              boxShadow: '0 0 12px rgba(255, 255, 255, 0.8)',
            }}
            initial={{ scale: 0, opacity: 0 }}
            animate={{
              scale: [1, 1.2, 1],
              opacity: 1,
            }}
            transition={{
              scale: {
                duration: 2,
                repeat: Infinity,
                ease: 'easeInOut',
              },
              opacity: {
                duration: 0.5,
              },
            }}
            onClick={() => {
              onEventTap(event);
              onHaptic?.();
            }}
          />
        ))}
      </motion.div>
    </div>
  );
}