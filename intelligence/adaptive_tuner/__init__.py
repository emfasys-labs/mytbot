"""Adaptive Tuner — live, bounded, regime-conditioned parameter self-tuning.

Observes myTbot's own realized performance, attributes it to the parameter
values in force, and proposes bounded nudges toward better historical
performance — applied live (no redeploy) within hard safety rails. An optional
AI advisor (Gemini), grounded with the system's own recent history, proposes
*which* parameter to focus on and explains the change; the bounded statistical
optimizer always decides the actual magnitude.
"""

from intelligence.adaptive_tuner.service import AdaptiveTunerService

__all__ = ["AdaptiveTunerService"]
