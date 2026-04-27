"""
governance/
=============
Wave 14 — paper-soak + activation gates.

Encodes the activation rules from the strategy/AI roadmap as runtime
checks, so a model or strategy CANNOT be flipped to live without the
gates passing in code (not just docs).
"""

from governance.activation_gates import (
    ActivationContext,
    ActivationGate,
    ActivationGateResult,
    ActivationGates,
    ActivationVerdict,
    evaluate_activation,
)

__all__ = [
    "ActivationContext",
    "ActivationGate",
    "ActivationGateResult",
    "ActivationGates",
    "ActivationVerdict",
    "evaluate_activation",
]
