"""
portfolio/adaptive_sizing.py
============================
Dynamic position sizing constraints replacing hardcoded absolute max weights.
Computes an adaptive ceiling per opportunity based on volatility, liquidity, and regime.
"""

from decimal import Decimal
from core.models_runtime import Opportunity, RegimeState, ProfileMode, clip_decimal, PortfolioState
from config.models import ProfileModesConfig

def compute_adaptive_max_weight(
    opportunity: Opportunity,
    regime_state: RegimeState,
    portfolio_state: PortfolioState,
    mode: ProfileMode,
    profile_cfg: ProfileModesConfig,
    target_risk_budget: float = 0.015, # 1.5% NAV risk budget
) -> Decimal:
    """
    Computes a dynamic maximum weight for a specific opportunity.
    Combines volatility constraint (Risk Parity), liquidity capacity, and safety bounds.
    """
    # 1. Base absolute upper bound (e.g., 1.0 for hunter, 0.5 for trader)
    nuclear_max = float(profile_cfg.safety_bounds.absolute_max_single_position_weight.get(mode, 1.0))
    
    meta = opportunity.metadata if isinstance(opportunity.metadata, dict) else {}
    
    # 2. Risk Parity Volatility Constraint
    # We want position_weight * vol_pct <= target_risk_budget
    vol_cap = nuclear_max
    try:
        atr = float(meta.get("atr_14", 0.0) or 0.0)
        close_px = float(meta.get("close", 0.0) or 0.0)
        if close_px <= 0:
            close_px = float(opportunity.price)
        if close_px > 0 and atr > 0:
            vol_pct = atr / close_px
            if vol_pct > 0:
                calculated_vol_cap = target_risk_budget / vol_pct
                vol_cap = min(nuclear_max, calculated_vol_cap)
    except (TypeError, ValueError, AttributeError):
        pass
        
    # 3. Liquidity Constraint
    liq_cap = vol_cap
    try:
        if opportunity.components is not None:
            liq_score = float(opportunity.components.liquidity_quality)
            liq_multiplier = max(0.2, min(1.0, liq_score * 1.5)) 
            liq_cap = vol_cap * liq_multiplier
    except (TypeError, ValueError, AttributeError):
        pass
        
    # 4. Regime Scaling
    regime_cap = liq_cap
    if regime_state is not None:
        regime_label = regime_state.regime_label
        if regime_label in ("crash", "panic"):
            regime_cap *= 0.25
        elif regime_label in ("risk_off", "volatile"):
            regime_cap *= 0.50
        
    # 4.5 Semantic Correlation Penalty
    correlation_penalty = 1.0
    if portfolio_state and portfolio_state.positions:
        opp_sector = str(meta.get("sector", "")).strip().lower()
        opp_industry = str(meta.get("industry", "")).strip().lower()
        opp_ac = str(opportunity.asset_class).strip().lower()
        
        industry_overlap = 0
        sector_overlap = 0
        ac_overlap = 0
        total_positions = len(portfolio_state.positions)
        
        for pos in portfolio_state.positions:
            pos_meta = pos.metadata if isinstance(pos.metadata, dict) else {}
            p_sector = str(pos_meta.get("sector", "")).strip().lower()
            p_industry = str(pos_meta.get("industry", "")).strip().lower()
            p_ac = str(pos.asset_class).strip().lower()
            
            if opp_industry and opp_industry == p_industry:
                industry_overlap += 1
            if opp_sector and opp_sector == p_sector:
                sector_overlap += 1
            if opp_ac and opp_ac == p_ac:
                ac_overlap += 1
                
        if total_positions > 0:
            if opp_industry and (industry_overlap / total_positions) > 0.2:
                correlation_penalty *= 0.5
            elif opp_sector and (sector_overlap / total_positions) > 0.3:
                correlation_penalty *= 0.6
                
            if opp_ac in ("forex", "fx", "crypto") and not opp_sector:
                if (ac_overlap / total_positions) > 0.3:
                    correlation_penalty *= 0.7
                    
    regime_cap *= correlation_penalty

    # 5. Conviction Scaling
    try:
        confidence = float(opportunity.confidence)
        confidence = max(0.25, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 1.0
        
    final_cap = regime_cap * confidence
    
    # 6. Final safety clip
    final_cap = max(0.01, min(nuclear_max, final_cap))
    final_cap_str = f"{final_cap:.4f}"
    return clip_decimal(Decimal(final_cap_str), Decimal("0"), Decimal(str(nuclear_max)))
