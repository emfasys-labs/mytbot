import { motion, AnimatePresence } from 'motion/react';
import { useEffect, useState } from 'react';

type HapticIntensity = 'light' | 'medium' | 'heavy';

interface HapticFeedbackProps {
  intensity?: HapticIntensity;
}

let hapticTrigger: ((intensity?: HapticIntensity) => void) | null = null;

export function useHaptic() {
  return (intensity?: HapticIntensity) => {
    if (hapticTrigger) {
      hapticTrigger(intensity);
    }
  };
}

export function HapticFeedback({ intensity = 'medium' }: HapticFeedbackProps) {
  const [pulses, setPulses] = useState<{ id: number; intensity: HapticIntensity }[]>([]);
  const [nextId, setNextId] = useState(0);

  useEffect(() => {
    hapticTrigger = (triggerIntensity = intensity) => {
      const id = Date.now() + Math.random();
      setPulses((prev) => [...prev, { id, intensity: triggerIntensity }]);
      setNextId((prev) => prev + 1);

      // Remove pulse after animation
      setTimeout(() => {
        setPulses((prev) => prev.filter((p) => p.id !== id));
      }, 300);
    };

    return () => {
      hapticTrigger = null;
    };
  }, [intensity]);

  const getScale = (intensity: HapticIntensity) => {
    switch (intensity) {
      case 'light':
        return 1.02;
      case 'medium':
        return 1.04;
      case 'heavy':
        return 1.08;
    }
  };

  return (
    <AnimatePresence>
      {pulses.map((pulse) => (
        <motion.div
          key={pulse.id}
          className="fixed inset-0 pointer-events-none z-50"
          initial={{ opacity: 0 }}
          animate={{ opacity: [0, 0.1, 0] }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.3, ease: 'easeOut' }}
        >
          <motion.div
            className="absolute inset-0 bg-white/5"
            initial={{ scale: 1 }}
            animate={{ scale: getScale(pulse.intensity) }}
            transition={{ duration: 0.15, ease: 'easeOut' }}
          />
        </motion.div>
      ))}
    </AnimatePresence>
  );
}
