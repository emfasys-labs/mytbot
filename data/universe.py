from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger


@dataclass
class UniverseInstrument:
    symbol: str
    name: str
    asset_class: str
    broker: str
    broker_symbol: str
    sector: Optional[str]
    region: Optional[str]
    is_trigger: bool = True
    is_target: bool = True
    scan_enabled: bool = True
    added_reason: str = "initial_universe"


class UniverseManager:
    INITIAL_UNIVERSE = [
        UniverseInstrument("SPY", "S&P 500 ETF", "etf", "ibkr", "SPY", "broad_market", "US"),
        UniverseInstrument("QQQ", "Nasdaq 100 ETF", "etf", "ibkr", "QQQ", "technology", "US"),
        UniverseInstrument("IWM", "Russell 2000 ETF", "etf", "ibkr", "IWM", "small_cap", "US"),
        UniverseInstrument("VIX", "Volatility Index", "index", "ibkr", "VIX", None, "US"),
        UniverseInstrument("DXY", "US Dollar Index", "forex", "ibkr", "DXY", None, "US"),
        UniverseInstrument("TLT", "20yr Treasury ETF", "etf", "ibkr", "TLT", "bonds", "US"),
        UniverseInstrument("IEF", "10yr Treasury ETF", "etf", "ibkr", "IEF", "bonds", "US"),
        UniverseInstrument("HYG", "High Yield Bond ETF", "etf", "ibkr", "HYG", "bonds", "US"),
        UniverseInstrument("TIPS", "Inflation-Protected ETF", "etf", "ibkr", "TIPS", "bonds", "US"),
        UniverseInstrument("GLD", "Gold ETF", "etf", "ibkr", "GLD", "commodities", "Global"),
        UniverseInstrument("SLV", "Silver ETF", "etf", "ibkr", "SLV", "commodities", "Global"),
        UniverseInstrument("USO", "Oil ETF", "etf", "ibkr", "USO", "commodities", "Global"),
        UniverseInstrument("UNG", "Natural Gas ETF", "etf", "ibkr", "UNG", "commodities", "Global"),
        UniverseInstrument("CORN", "Corn ETF", "etf", "ibkr", "CORN", "commodities", "Global"),
        UniverseInstrument("WEAT", "Wheat ETF", "etf", "ibkr", "WEAT", "commodities", "Global"),
        UniverseInstrument("COPX", "Copper Miners ETF", "etf", "ibkr", "COPX", "commodities", "Global"),
        UniverseInstrument("XLE", "Energy Sector", "etf", "ibkr", "XLE", "energy", "US"),
        UniverseInstrument("XLF", "Financial Sector", "etf", "ibkr", "XLF", "financials", "US"),
        UniverseInstrument("XLK", "Technology Sector", "etf", "ibkr", "XLK", "technology", "US"),
        UniverseInstrument("XLV", "Healthcare Sector", "etf", "ibkr", "XLV", "healthcare", "US"),
        UniverseInstrument("XLI", "Industrial Sector", "etf", "ibkr", "XLI", "industrials", "US"),
        UniverseInstrument("XLY", "Consumer Discret.", "etf", "ibkr", "XLY", "consumer", "US"),
        UniverseInstrument("XLP", "Consumer Staples", "etf", "ibkr", "XLP", "staples", "US"),
        UniverseInstrument("XLU", "Utilities Sector", "etf", "ibkr", "XLU", "utilities", "US"),
        UniverseInstrument("XLB", "Materials Sector", "etf", "ibkr", "XLB", "materials", "US"),
        UniverseInstrument("XLRE", "Real Estate Sector", "etf", "ibkr", "XLRE", "real_estate", "US"),
        UniverseInstrument("XLC", "Communications", "etf", "ibkr", "XLC", "comms", "US"),
        UniverseInstrument("EEM", "Emerging Markets ETF", "etf", "ibkr", "EEM", "em", "Global"),
        UniverseInstrument("FXI", "China Large Cap ETF", "etf", "ibkr", "FXI", "china", "Asia"),
        UniverseInstrument("EWJ", "Japan ETF", "etf", "ibkr", "EWJ", "japan", "Asia"),
        UniverseInstrument("EWG", "Germany ETF", "etf", "ibkr", "EWG", "europe", "EU"),
        UniverseInstrument("EWU", "UK ETF", "etf", "ibkr", "EWU", "uk", "EU"),
        UniverseInstrument("VWO", "EM ETF (Vanguard)", "etf", "ibkr", "VWO", "em", "Global"),
        UniverseInstrument("AAPL", "Apple", "equity", "ibkr", "AAPL", "technology", "US"),
        UniverseInstrument("MSFT", "Microsoft", "equity", "ibkr", "MSFT", "technology", "US"),
        UniverseInstrument("NVDA", "Nvidia", "equity", "ibkr", "NVDA", "semiconductors", "US"),
        UniverseInstrument("TSLA", "Tesla", "equity", "ibkr", "TSLA", "ev", "US"),
        UniverseInstrument("JPM", "JPMorgan Chase", "equity", "ibkr", "JPM", "financials", "US"),
        UniverseInstrument("XOM", "Exxon Mobil", "equity", "ibkr", "XOM", "energy", "US"),
        UniverseInstrument("CVX", "Chevron", "equity", "ibkr", "CVX", "energy", "US"),
        UniverseInstrument("UAL", "United Airlines", "equity", "ibkr", "UAL", "airlines", "US"),
        UniverseInstrument("DAL", "Delta Airlines", "equity", "ibkr", "DAL", "airlines", "US"),
        UniverseInstrument("LMT", "Lockheed Martin", "equity", "ibkr", "LMT", "defence", "US"),
        UniverseInstrument("RTX", "Raytheon", "equity", "ibkr", "RTX", "defence", "US"),
        UniverseInstrument("COIN", "Coinbase", "equity", "ibkr", "COIN", "crypto", "US"),
        UniverseInstrument("MSTR", "MicroStrategy", "equity", "ibkr", "MSTR", "crypto", "US"),
        UniverseInstrument("MARA", "Marathon Digital", "equity", "ibkr", "MARA", "crypto", "US"),
        UniverseInstrument("GDX", "Gold Miners ETF", "etf", "ibkr", "GDX", "mining", "Global"),
        UniverseInstrument("SMH", "Semiconductors ETF", "etf", "ibkr", "SMH", "semiconductors", "US"),
        UniverseInstrument("EUR_USD", "Euro/Dollar", "forex", "ibkr", "EUR.USD", "major", "Global"),
        UniverseInstrument("GBP_USD", "Pound/Dollar", "forex", "ibkr", "GBP.USD", "major", "Global"),
        UniverseInstrument("USD_JPY", "Dollar/Yen", "forex", "ibkr", "USD.JPY", "major", "Global"),
        UniverseInstrument("AUD_USD", "Aussie/Dollar", "forex", "ibkr", "AUD.USD", "commodity_fx", "Global"),
        UniverseInstrument("USD_CAD", "Dollar/Canadian", "forex", "ibkr", "USD.CAD", "commodity_fx", "Global"),
        UniverseInstrument("USD_NOK", "Dollar/Krone", "forex", "ibkr", "USD.NOK", "commodity_fx", "Global"),
        UniverseInstrument("BTC", "Bitcoin", "crypto", "kraken", "XBT/USD", "crypto", "Global"),
        UniverseInstrument("ETH", "Ethereum", "crypto", "kraken", "ETH/USD", "crypto", "Global"),
        UniverseInstrument("SOL", "Solana", "crypto", "kraken", "SOL/USD", "crypto", "Global"),
        UniverseInstrument("ADA", "Cardano", "crypto", "kraken", "ADA/USD", "crypto", "Global"),
        UniverseInstrument("LINK", "Chainlink", "crypto", "kraken", "LINK/USD", "crypto", "Global"),
    ]

    def __init__(self) -> None:
        self._universe: dict[str, UniverseInstrument] = {x.symbol: x for x in self.INITIAL_UNIVERSE}
        logger.info("Universe initialised with {} instruments", len(self._universe))

    def get_all(self) -> list[UniverseInstrument]:
        return list(self._universe.values())

    def get_triggers(self) -> list[UniverseInstrument]:
        return [x for x in self._universe.values() if x.is_trigger]

    def get_targets(self) -> list[UniverseInstrument]:
        return [x for x in self._universe.values() if x.is_target]

    def get_by_asset_class(self, asset_class: str) -> list[UniverseInstrument]:
        k = asset_class.strip().lower()
        return [x for x in self._universe.values() if x.asset_class.strip().lower() == k]

    def get_by_sector(self, sector: str) -> list[UniverseInstrument]:
        k = sector.strip().lower()
        return [x for x in self._universe.values() if (x.sector or "").strip().lower() == k]

    def add_instrument(self, instrument: UniverseInstrument, reason: str = "dependency_graph") -> None:
        if instrument.symbol in self._universe:
            return
        instrument.added_reason = reason
        self._universe[instrument.symbol] = instrument
        logger.info(
            "UNIVERSE EXPANDED | added {} | reason={} | total={}",
            instrument.symbol,
            reason,
            len(self._universe),
        )

    def __len__(self) -> int:
        return len(self._universe)
