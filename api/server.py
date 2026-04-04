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
    return {
        "status": "running",
        "mode": APP_ENV,
        "paper_mode": APP_ENV != "live",
        "kill_switch": False,           # TODO M7: read from risk engine
        "connected_brokers": [],        # TODO M7: read from execution engine
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
    # TODO M7: call risk_engine.kill() and execution_engine.cancel_all()
    return {"kill_switch": True, "message": "Kill switch activated"}


@app.post("/kill/reset")
async def reset_kill_switch():
    """Re-enable trading after kill switch. Deliberate action required."""
    # TODO M7: call risk_engine.reset_kill()
    return {"kill_switch": False, "message": "Kill switch reset"}


@app.post("/strategy/{name}/toggle")
async def toggle_strategy(name: str):
    """Enable or disable a strategy by name."""
    # TODO M7: toggle in strategy registry
    return {"strategy": name, "message": f"Strategy {name} toggled"}
