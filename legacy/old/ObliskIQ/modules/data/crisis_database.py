"""
Historical Crisis Database

PURPOSE:
1. Catalog major market crises for pattern recognition
2. Improve regime detection during volatile periods
3. Enable crisis-aware position sizing
4. Validate backtest performance during stress periods

MAJOR CRISES CATALOGUED:
- 1987: Black Monday (-20% single day)
- 2000-2002: Dot-com Bubble Burst (-78% Nasdaq)
- 2008-2009: Global Financial Crisis (-57% S&P 500)
- 2011: European Debt Crisis
- 2015: China Stock Market Crash
- 2018 Q4: Rate Hike Selloff (-20%)
- 2020: COVID-19 Crash (-34% in 33 days)
- 2022: Inflation/Rate Hike (-25%)

CRISIS TYPES:
- CREDIT: Credit market freeze (2008)
- PANDEMIC: Health crisis (2020)
- GEOPOLITICAL: War, terrorism
- MONETARY: Central bank policy shock (2022)
- SPECULATIVE: Bubble burst (2000, 2015)
- FLASH: Sudden technical crash (1987, Flash Crash 2010)

USAGE:
    crisis_db = CrisisDatabase()

    # Load historical crises
    crisis_db.populate_historical_crises()

    # Check if date is in crisis
    is_crisis, crisis_info = crisis_db.is_crisis_period('2020-03-15')

    # Get crisis severity
    severity = crisis_db.get_crisis_severity('2020-03-15')  # Returns 0-10
"""

import logging
import pandas as pd
from datetime import date
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass
from enum import Enum
import sys
import os

# Import warehouse
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from modules.data.warehouse import DataWarehouse

logger = logging.getLogger(__name__)


class CrisisType(Enum):
    """Types of market crises"""

    CREDIT = "credit"  # Credit market freeze
    PANDEMIC = "pandemic"  # Health crisis
    GEOPOLITICAL = "geopolitical"  # War, terrorism
    MONETARY = "monetary"  # Central bank policy shock
    SPECULATIVE = "speculative"  # Bubble burst
    FLASH = "flash"  # Sudden technical crash
    SOVEREIGN = "sovereign"  # Sovereign debt crisis
    COMMODITY = "commodity"  # Oil shock, commodity crisis


@dataclass
class Crisis:
    """Single crisis event"""

    name: str
    crisis_type: CrisisType
    start_date: date
    end_date: date
    peak_drawdown_pct: float  # Maximum drawdown during crisis
    severity: int  # 1-10 scale
    regions_affected: List[str]  # ['US', 'EUR', 'GLOBAL']
    key_indicators: Dict[str, Any]  # VIX spike, credit spreads, etc.
    description: str
    recovery_months: int  # Months to recover to pre-crisis levels


class CrisisDatabase:
    """
    Manage historical crisis database

    Provides crisis detection, classification, and historical lookup
    """

    def __init__(self, warehouse: Optional[DataWarehouse] = None):
        """
        Initialize crisis database

        Args:
            warehouse: DataWarehouse instance
        """
        self.warehouse = warehouse or DataWarehouse()
        logger.info("CrisisDatabase initialized")

    def populate_historical_crises(self):
        """
        Populate database with major historical crises

        Run this once to initialize crisis database
        """
        logger.info("Populating historical crisis database...")

        crises = [
            Crisis(
                name="Black Monday 1987",
                crisis_type=CrisisType.FLASH,
                start_date=date(1987, 10, 19),
                end_date=date(1987, 10, 19),
                peak_drawdown_pct=-22.6,  # Single day drop
                severity=9,
                regions_affected=["GLOBAL"],
                key_indicators={"vix_spike": True, "program_trading": True},
                description="Single-day 22.6% crash in S&P 500 due to program trading and portfolio insurance",
                recovery_months=20,
            ),
            Crisis(
                name="Dot-com Bubble Burst",
                crisis_type=CrisisType.SPECULATIVE,
                start_date=date(2000, 3, 10),
                end_date=date(2002, 10, 9),
                peak_drawdown_pct=-78.0,  # Nasdaq peak to trough
                severity=8,
                regions_affected=["US", "GLOBAL"],
                key_indicators={"pe_ratios_extreme": True, "tech_bubble": True},
                description="Technology bubble burst, Nasdaq lost 78% from peak",
                recovery_months=156,  # Took 13 years for Nasdaq to recover
            ),
            Crisis(
                name="Global Financial Crisis",
                crisis_type=CrisisType.CREDIT,
                start_date=date(2007, 10, 9),
                end_date=date(2009, 3, 9),
                peak_drawdown_pct=-56.8,  # S&P 500 peak to trough
                severity=10,  # Maximum severity
                regions_affected=["GLOBAL"],
                key_indicators={
                    "lehman_bankruptcy": True,
                    "credit_freeze": True,
                    "ted_spread": 4.58,  # Peak TED spread
                    "vix_peak": 80.86,
                },
                description="Subprime mortgage crisis led to global credit freeze, Lehman bankruptcy",
                recovery_months=49,
            ),
            Crisis(
                name="Flash Crash 2010",
                crisis_type=CrisisType.FLASH,
                start_date=date(2010, 5, 6),
                end_date=date(2010, 5, 6),
                peak_drawdown_pct=-9.2,  # Intraday drop
                severity=5,
                regions_affected=["US"],
                key_indicators={"algorithmic_trading": True, "liquidity_vacuum": True},
                description="Intraday crash caused by algorithmic trading, recovered within hours",
                recovery_months=0,  # Recovered same day
            ),
            Crisis(
                name="European Debt Crisis",
                crisis_type=CrisisType.SOVEREIGN,
                start_date=date(2010, 5, 1),
                end_date=date(2012, 7, 26),
                peak_drawdown_pct=-21.6,  # S&P 500 (US impact)
                severity=7,
                regions_affected=["EUR", "GLOBAL"],
                key_indicators={
                    "greek_default_risk": True,
                    "italian_spanish_spreads": True,
                    "ecb_intervention": "LTRO",
                },
                description="Greece, Italy, Spain sovereign debt crisis, ECB intervention required",
                recovery_months=14,
            ),
            Crisis(
                name="China Stock Market Crash 2015",
                crisis_type=CrisisType.SPECULATIVE,
                start_date=date(2015, 6, 12),
                end_date=date(2016, 1, 27),
                peak_drawdown_pct=-43.0,  # Shanghai Composite
                severity=6,
                regions_affected=["CHN", "EMERGING"],
                key_indicators={
                    "margin_debt_collapse": True,
                    "circuit_breakers": True,
                    "capital_controls": True,
                },
                description="Chinese stock bubble burst, margin debt collapse, circuit breakers triggered",
                recovery_months=18,
            ),
            Crisis(
                name="Q4 2018 Selloff",
                crisis_type=CrisisType.MONETARY,
                start_date=date(2018, 10, 3),
                end_date=date(2018, 12, 24),
                peak_drawdown_pct=-19.8,  # S&P 500 (just shy of bear market)
                severity=5,
                regions_affected=["US", "GLOBAL"],
                key_indicators={
                    "fed_rate_hikes": 4,  # 4 hikes in 2018
                    "quantitative_tightening": True,
                    "trade_war": "US-China",
                },
                description="Fed rate hikes + balance sheet reduction + trade war fears",
                recovery_months=4,
            ),
            Crisis(
                name="COVID-19 Crash",
                crisis_type=CrisisType.PANDEMIC,
                start_date=date(2020, 2, 19),
                end_date=date(2020, 3, 23),
                peak_drawdown_pct=-33.9,  # S&P 500, fastest 30%+ drop in history
                severity=9,
                regions_affected=["GLOBAL"],
                key_indicators={
                    "vix_peak": 82.69,  # All-time high
                    "oil_crash": -300,  # WTI went negative
                    "fed_emergency_cuts": "0-0.25%",
                    "global_lockdowns": True,
                },
                description="Global pandemic, fastest bear market in history (33 days), unprecedented fiscal/monetary response",
                recovery_months=5,  # Fastest recovery due to Fed intervention
            ),
            Crisis(
                name="2022 Inflation/Rate Hike Selloff",
                crisis_type=CrisisType.MONETARY,
                start_date=date(2022, 1, 3),
                end_date=date(2022, 10, 12),
                peak_drawdown_pct=-25.4,  # S&P 500
                severity=6,
                regions_affected=["US", "GLOBAL"],
                key_indicators={
                    "inflation_peak": 9.1,  # CPI peak
                    "fed_hikes": 7,  # 7 consecutive hikes (425 bps total)
                    "nasdaq_crash": -33.7,  # Tech got hit hardest
                    "bond_crash": -16.0,  # TLT worst year ever
                },
                description="Inflation surge to 40-year high, aggressive Fed tightening, bonds and stocks crashed together",
                recovery_months=6,
            ),
            Crisis(
                name="Russia-Ukraine War Shock",
                crisis_type=CrisisType.GEOPOLITICAL,
                start_date=date(2022, 2, 24),
                end_date=date(2022, 3, 14),
                peak_drawdown_pct=-12.5,  # S&P 500 from pre-invasion peak
                severity=5,
                regions_affected=["EUR", "GLOBAL"],
                key_indicators={
                    "oil_spike": 130,  # WTI peak
                    "nat_gas_spike": 800,  # EU gas prices (% increase)
                    "sanctions": "SWIFT cutoff",
                    "wheat_crisis": True,
                },
                description="Russia invades Ukraine, energy crisis in Europe, commodity price shocks",
                recovery_months=2,
            ),
        ]

        # Store in database
        for crisis in crises:
            self._store_crisis(crisis)

        logger.info(f"Stored {len(crises)} historical crises in database")

    def _store_crisis(self, crisis: Crisis):
        """Store crisis in database"""
        conn = self.warehouse.conn

        # Insert crisis
        conn.execute(
            """
            INSERT OR REPLACE INTO historical_crises
            (name, crisis_type, start_date, end_date, peak_drawdown_pct, severity,
             regions_affected, description, recovery_months)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                crisis.name,
                crisis.crisis_type.value,
                crisis.start_date,
                crisis.end_date,
                crisis.peak_drawdown_pct,
                crisis.severity,
                ",".join(crisis.regions_affected),
                crisis.description,
                crisis.recovery_months,
            ),
        )

        conn.commit()

    def is_crisis_period(
        self, check_date: str, min_severity: int = 5
    ) -> Tuple[bool, Optional[Dict]]:
        """
        Check if date falls within a crisis period

        Args:
            check_date: Date to check (YYYY-MM-DD)
            min_severity: Minimum severity to consider (1-10)

        Returns:
            Tuple of (is_crisis: bool, crisis_info: dict or None)
        """
        query = """
            SELECT name, crisis_type, start_date, end_date, peak_drawdown_pct,
                   severity, regions_affected, description
            FROM historical_crises
            WHERE ? BETWEEN start_date AND end_date
            AND severity >= ?
        """

        result = self.warehouse.conn.execute(query, (check_date, min_severity)).fetchone()

        if result:
            crisis_info = {
                "name": result[0],
                "crisis_type": result[1],
                "start_date": result[2],
                "end_date": result[3],
                "peak_drawdown_pct": result[4],
                "severity": result[5],
                "regions_affected": result[6].split(","),
                "description": result[7],
            }
            return True, crisis_info
        else:
            return False, None

    def get_crisis_severity(self, check_date: str) -> int:
        """
        Get crisis severity for a date (0-10)

        Args:
            check_date: Date to check

        Returns:
            Severity score (0 = no crisis, 10 = maximum severity)
        """
        is_crisis, crisis_info = self.is_crisis_period(check_date, min_severity=1)

        if is_crisis and crisis_info is not None:
            return crisis_info["severity"]
        else:
            return 0

    def get_all_crises(
        self, crisis_type: Optional[str] = None, min_severity: int = 1
    ) -> pd.DataFrame:
        """
        Retrieve all crises from database

        Args:
            crisis_type: Filter by type (optional)
            min_severity: Minimum severity

        Returns:
            DataFrame with all crises
        """
        query = "SELECT * FROM historical_crises WHERE severity >= ?"
        params: List[Any] = [min_severity]

        if crisis_type:
            query += " AND crisis_type = ?"
            params.append(crisis_type)

        query += " ORDER BY start_date DESC"

        df = pd.read_sql_query(query, self.warehouse.conn, params=params)
        return df

    def get_crisis_statistics(self) -> Dict[str, Any]:
        """
        Calculate statistics about historical crises

        Returns:
            Dict with crisis statistics
        """
        df = self.get_all_crises()

        stats = {
            "total_crises": len(df),
            "avg_severity": df["severity"].mean(),
            "avg_drawdown_pct": df["peak_drawdown_pct"].mean(),
            "avg_recovery_months": df["recovery_months"].mean(),
            "max_drawdown_pct": df["peak_drawdown_pct"].min(),  # Most negative
            "fastest_recovery_months": df["recovery_months"].min(),
            "slowest_recovery_months": df["recovery_months"].max(),
            "crisis_types": df["crisis_type"].value_counts().to_dict(),
            "most_common_type": df["crisis_type"].mode().values[0] if not df.empty else None,
        }

        return stats

    def add_crisis_to_schema(self):
        """
        Add historical_crises table to database schema

        Run this once to create the table
        """
        conn = self.warehouse.conn

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS historical_crises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                crisis_type TEXT NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                peak_drawdown_pct REAL,
                severity INTEGER CHECK(severity BETWEEN 1 AND 10),
                regions_affected TEXT,  -- Comma-separated: US,EUR,GLOBAL
                description TEXT,
                recovery_months INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crisis_dates
            ON historical_crises(start_date, end_date)
        """
        )

        conn.commit()
        logger.info("Created historical_crises table")


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    crisis_db = CrisisDatabase()

    # Create table (run once)
    crisis_db.add_crisis_to_schema()

    # Populate historical crises
    crisis_db.populate_historical_crises()

    # Check if specific date was in crisis
    is_crisis, info = crisis_db.is_crisis_period("2020-03-15")
    if is_crisis and info is not None:
        print(f"\n2020-03-15 was during: {info['name']}")
        print(f"  Type: {info['crisis_type']}")
        print(f"  Severity: {info['severity']}/10")
        print(f"  Peak Drawdown: {info['peak_drawdown_pct']:.1f}%")
        print(f"  Description: {info['description']}")

    # Get all crises
    df = crisis_db.get_all_crises()
    print(f"\n{len(df)} historical crises catalogued")

    # Statistics
    stats = crisis_db.get_crisis_statistics()
    print(f"\nCrisis Statistics:")
    print(f"  Average Severity: {stats['avg_severity']:.1f}/10")
    print(f"  Average Drawdown: {stats['avg_drawdown_pct']:.1f}%")
    print(f"  Average Recovery: {stats['avg_recovery_months']:.0f} months")
    print(f"  Worst Drawdown: {stats['max_drawdown_pct']:.1f}%")
    print(f"  Crisis Types: {stats['crisis_types']}")
