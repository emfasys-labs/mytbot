"""
api/server.py
==============
FastAPI backend for the control dashboard.

Endpoints:
    GET  /status          — system health and mode
    GET  /positions       — all open positions
    GET  /orders          — recent orders
    GET  /signals         — recent signals with rationale
    GET  /pnl             — P&L summary
    POST /kill            — activate kill switch
    POST /kill/reset      — deactivate kill switch
    POST /strategy/{name}/toggle  — enable/disable a strategy

Run: uvicorn api.server:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import os

from control.runtime import get_execution_engine, get_risk_engine

app = FastAPI(
    title="mytbot Control API",
    description="Autonomous trading system dashboard API",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # lock this down in production
    allow_methods=["*"],
    allow_headers=["*"],
)

APP_ENV = os.getenv("APP_ENV", "paper")


@app.get("/status")
async def get_status():
    risk_engine = get_risk_engine()
    execution_engine = get_execution_engine()
    connected_brokers = []
    if execution_engine is not None:
        connected_brokers = list(getattr(execution_engine, "_brokers", {}).keys())
    return {
        "status": "running",
        "mode": APP_ENV,
        "paper_mode": APP_ENV != "live",
        "kill_switch": bool(getattr(risk_engine, "is_killed", False)),
        "connected_brokers": connected_brokers,
        "active_strategies": [],        # TODO M7: read from strategy registry
    }


@app.get("/positions")
async def get_positions():
    # TODO M7: query from database
    return {"positions": []}


@app.get("/orders")
async def get_orders(limit: int = 50):
    # TODO M7: query from database
    return {"orders": []}


@app.get("/signals")
async def get_signals(limit: int = 50):
    # TODO M7: query from database
    return {"signals": []}


@app.get("/pnl")
async def get_pnl():
    # TODO M7: query from database
    return {
        "today": {"realised": 0, "unrealised": 0, "fees": 0, "trades": 0},
        "all_time": {"realised": 0, "unrealised": 0, "fees": 0, "trades": 0},
    }


@app.post("/kill")
async def activate_kill_switch():
    """Immediately halt all trading. Cancel all open orders."""
    risk_engine = get_risk_engine()
    execution_engine = get_execution_engine()
    if risk_engine is None:
        raise HTTPException(status_code=503, detail="Risk engine not registered")

    risk_engine.kill()
    if execution_engine is not None:
        await execution_engine.cancel_all()
    return {"kill_switch": True, "message": "Kill switch activated; open orders cancelled"}


@app.post("/kill/reset")
async def reset_kill_switch():
    """Re-enable trading after kill switch. Deliberate action required."""
    risk_engine = get_risk_engine()
    if risk_engine is None:
        raise HTTPException(status_code=503, detail="Risk engine not registered")
    risk_engine.reset_kill()
    return {"kill_switch": False, "message": "Kill switch reset"}


@app.post("/strategy/{name}/toggle")
async def toggle_strategy(name: str):
    """Enable or disable a strategy by name."""
    # TODO M7: toggle in strategy registry
    return {"strategy": name, "message": f"Strategy {name} toggled"}
