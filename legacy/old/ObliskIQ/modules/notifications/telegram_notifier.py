"""
Telegram Notification System
============================
Sends portfolio updates and alerts via Telegram bot.

Setup:
1. Create bot: Talk to @BotFather on Telegram
2. Get bot token: Will look like "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
3. Get your chat ID: Message bot, then visit:
   https://api.telegram.org/bot<YourBOTToken>/getUpdates
4. Set environment variables:
   TELEGRAM_BOT_TOKEN=your_token
   TELEGRAM_CHAT_ID=your_chat_id

Usage:
    from modules.notifications.telegram_notifier import TelegramNotifier

    notifier = TelegramNotifier()
    notifier.send_morning_report(signals=5, bullish=3, bearish=2, avg_ic=0.62)
    notifier.send_evening_report(return_pct=0.5, health="GOOD")
"""

import os
import requests
from typing import List, Optional
from datetime import datetime
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class PortfolioSnapshot:
    """Portfolio snapshot for notifications"""

    timestamp: datetime
    portfolio_value: float
    daily_return_pct: float
    total_return_pct: float
    cash_pct: float
    num_positions: int
    sharpe_ratio: float
    max_drawdown_pct: float
    health_status: str  # HEALTHY, DEGRADED, CRITICAL
    correlation: float


@dataclass
class SignalSummary:
    """Signal summary for notifications"""

    timestamp: datetime
    total_signals: int
    bullish_signals: int
    bearish_signals: int
    neutral_signals: int
    avg_ic: float
    top_patterns: List[str]
    recommended_actions: List[str]


class TelegramNotifier:
    """
    Send notifications via Telegram bot.

    Free tier: Unlimited messages
    Platform: iOS, Android, Web, Desktop
    Setup time: 5 minutes
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True,
    ):
        """
        Initialize Telegram notifier.

        Args:
            bot_token: Telegram bot token (or use TELEGRAM_BOT_TOKEN env var)
            chat_id: Telegram chat ID (or use TELEGRAM_CHAT_ID env var)
            enabled: Enable/disable notifications (default: True)
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = enabled and self.bot_token and self.chat_id

        if not self.enabled:
            logger.warning(
                "⚠️  Telegram notifications disabled. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable."
            )

        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """
        Send text message via Telegram.

        Args:
            text: Message text (supports Markdown)
            parse_mode: "Markdown" or "HTML"

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled:
            logger.debug(f"Telegram disabled, skipping message: {text[:50]}...")
            return False

        try:
            response = requests.post(
                f"{self.api_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode},
                timeout=10,
            )

            if response.status_code == 200:
                logger.info("✅ Telegram message sent successfully")
                return True
            else:
                logger.error(f"❌ Telegram API error: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"❌ Telegram send failed: {e}")
            return False

    def send_morning_report(
        self, signals: SignalSummary, portfolio: Optional[PortfolioSnapshot] = None
    ) -> bool:
        """
        Send morning report with today's signals.

        Args:
            signals: Signal summary
            portfolio: Optional portfolio snapshot

        Returns:
            True if sent successfully
        """
        timestamp = signals.timestamp.strftime("%Y-%m-%d %H:%M")

        # Build message
        message = f"🌅 *Morning Report* - {timestamp}\n\n"

        # Signals section
        message += "📊 *Market Signals*\n"
        message += f"• Total: {signals.total_signals} patterns triggered\n"
        message += f"• Bullish: {signals.bullish_signals} 📈\n"
        message += f"• Bearish: {signals.bearish_signals} 📉\n"
        message += f"• Neutral: {signals.neutral_signals} ➡️\n"
        message += f"• Avg IC: {signals.avg_ic:.2f} (predictive power)\n\n"

        # Top patterns
        if signals.top_patterns:
            message += "🎯 *Top Patterns*:\n"
            for i, pattern in enumerate(signals.top_patterns[:5], 1):
                message += f"{i}. {pattern}\n"
            message += "\n"

        # Recommended actions
        if signals.recommended_actions:
            message += "💡 *Recommended Actions*:\n"
            for action in signals.recommended_actions[:3]:
                message += f"• {action}\n"
            message += "\n"

        # Portfolio section (if provided)
        if portfolio:
            message += "💼 *Portfolio Status*\n"
            message += f"• Value: ${portfolio.portfolio_value:,.0f}\n"
            message += f"• Cash: {portfolio.cash_pct:.1f}%\n"
            message += f"• Positions: {portfolio.num_positions}\n"
            message += f"• Health: {portfolio.health_status}\n"

        message += "\n_Next update: Evening (6 PM)_"

        return self.send_message(message)

    def send_evening_report(
        self, portfolio: PortfolioSnapshot, updated_signals: Optional[SignalSummary] = None
    ) -> bool:
        """
        Send evening report with portfolio performance.

        Args:
            portfolio: Portfolio snapshot
            updated_signals: Optional updated signals (if patterns changed)

        Returns:
            True if sent successfully
        """
        timestamp = portfolio.timestamp.strftime("%Y-%m-%d %H:%M")

        # Build message
        message = f"🌙 *Evening Report* - {timestamp}\n\n"

        # Portfolio performance
        message += "💼 *Portfolio Performance*\n"
        message += f"• Value: ${portfolio.portfolio_value:,.0f}\n"

        # Daily return with emoji
        return_emoji = "📈" if portfolio.daily_return_pct > 0 else "📉"
        message += f"• Today: {portfolio.daily_return_pct:+.2f}% {return_emoji}\n"
        message += f"• Total Return: {portfolio.total_return_pct:+.2f}%\n"
        message += f"• Sharpe Ratio: {portfolio.sharpe_ratio:.2f}\n"
        message += f"• Max Drawdown: {portfolio.max_drawdown_pct:.2f}%\n\n"

        # Risk metrics
        message += "⚠️  *Risk Metrics*\n"
        message += f"• Correlation: {portfolio.correlation:.2f} "

        if portfolio.correlation < 0.3:
            message += "(LOW - excellent) ✅\n"
        elif portfolio.correlation < 0.6:
            message += "(MODERATE - acceptable) ⚠️\n"
        elif portfolio.correlation < 0.85:
            message += "(HIGH - concerning) 🚨\n"
        else:
            message += "(CRISIS - action required) 🔥\n"

        # Health status with emoji
        health_emoji = {
            "HEALTHY": "✅",
            "DEGRADED": "⚠️",
            "CRITICAL": "🚨",
        }
        message += f"• Health: {portfolio.health_status} {health_emoji.get(portfolio.health_status, '❓')}\n\n"

        # Updated signals (if provided)
        if updated_signals and updated_signals.total_signals > 0:
            message += "🔄 *Updated Signals (Closing Prices)*\n"
            message += f"• Total: {updated_signals.total_signals} patterns\n"
            message += f"• Avg IC: {updated_signals.avg_ic:.2f}\n\n"

        message += "_Next update: Tomorrow morning (9 AM)_"

        return self.send_message(message)

    def send_alert(self, level: str, title: str, message: str) -> bool:
        """
        Send urgent alert.

        Args:
            level: "INFO", "WARNING", "CRITICAL"
            title: Alert title
            message: Alert message

        Returns:
            True if sent successfully
        """
        emoji_map = {
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "CRITICAL": "🚨",
        }

        emoji = emoji_map.get(level.upper(), "❗")
        alert_text = f"{emoji} *{level.upper()}: {title}*\n\n{message}"

        return self.send_message(alert_text)

    def send_trade_recommendation(
        self, action: str, ticker: str, reason: str, confidence: float
    ) -> bool:
        """
        Send trade recommendation alert.

        Args:
            action: "BUY", "SELL", "REDUCE", "HOLD"
            ticker: Stock ticker
            reason: Reason for recommendation
            confidence: Confidence score (0-1)

        Returns:
            True if sent successfully
        """
        action_emoji = {
            "BUY": "🟢",
            "SELL": "🔴",
            "REDUCE": "🟡",
            "HOLD": "⚪",
        }

        emoji = action_emoji.get(action.upper(), "❔")
        confidence_pct = confidence * 100

        message = (
            f"{emoji} *Trade Recommendation*\n\n"
            f"• Action: *{action.upper()}* {ticker}\n"
            f"• Confidence: {confidence_pct:.0f}%\n"
            f"• Reason: {reason}\n"
        )

        return self.send_message(message)

    def send_weekly_drill_reminder(
        self,
        scenario_name: str,
        scenario_title: str,
        simulated_drawdown_pct: float,
        time_limit_minutes: int,
        severity: str,
    ) -> bool:
        """
        Send Sunday drill reminder.

        Ray Dalio: "Since human executes all trades, drill the recovery scenarios."

        Args:
            scenario_name: Internal scenario name
            scenario_title: Human-readable scenario title
            simulated_drawdown_pct: Simulated drawdown percentage
            time_limit_minutes: Time limit for the drill
            severity: Severity level (WARNING, CRITICAL, EMERGENCY)

        Returns:
            True if sent successfully
        """
        severity_emoji = {
            "WARNING": "🟡",
            "CRITICAL": "🟠",
            "EMERGENCY": "🔴",
        }

        emoji = severity_emoji.get(severity, "🏋️")

        message = f"""🏋️ *WEEKLY RECOVERY DRILL*

{emoji} *Scenario:* {scenario_title}
📉 *Simulated Drawdown:* {simulated_drawdown_pct}%
⏱️ *Time Limit:* {time_limit_minutes} minutes
⚠️ *Severity:* {severity}

*Instructions:*
1. Open `docs/HUMAN_RECOVERY_CHECKLIST.md`
2. Open `docs/RUNBOOK.md`
3. Start a timer
4. Execute the recovery procedure
5. Log your actions and time

*Why this matters:*
Real crises don't wait for you to read the docs.
Practice now so you're ready when it counts.

_Run: `python -m ObliskIQ.modules.testing.recovery_drill_simulator`_"""

        return self.send_message(message)

    def send_drill_result(
        self,
        scenario_title: str,
        score: float,
        passed: bool,
        time_taken_minutes: int,
        missed_actions: List[str],
    ) -> bool:
        """
        Send drill result notification.

        Args:
            scenario_title: Title of the drill scenario
            score: Score achieved (0-100)
            passed: Whether the drill was passed
            time_taken_minutes: Time taken to complete
            missed_actions: List of actions that were missed

        Returns:
            True if sent successfully
        """
        result_emoji = "✅" if passed else "❌"
        result_text = "PASSED" if passed else "FAILED"

        message = f"""{result_emoji} *Drill Result: {result_text}*

📊 *Scenario:* {scenario_title}
🎯 *Score:* {score:.1f}/100
⏱️ *Time:* {time_taken_minutes} minutes

"""

        if missed_actions:
            message += "*Missed Actions:*\n"
            for action in missed_actions[:5]:  # Limit to 5 for readability
                message += f"• {action}\n"

            if len(missed_actions) > 5:
                message += f"_...and {len(missed_actions) - 5} more_\n"

        if not passed:
            message += "\n_Review the runbook and try again next week._"
        else:
            message += "\n_Great job! Stay prepared._"

        return self.send_message(message)

    def test_connection(self) -> bool:
        """
        Test Telegram connection.

        Returns:
            True if connection successful
        """
        if not self.enabled:
            logger.error("❌ Telegram not configured")
            return False

        try:
            response = requests.get(f"{self.api_url}/getMe", timeout=10)

            if response.status_code == 200:
                data = response.json()
                bot_name = data.get("result", {}).get("username", "Unknown")
                logger.info(f"✅ Telegram connection successful: @{bot_name}")
                self.send_message("✅ ObliskIQ Telegram bot connected successfully!")
                return True
            else:
                logger.error(f"❌ Telegram API error: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"❌ Telegram connection failed: {e}")
            return False


# Example usage
if __name__ == "__main__":
    # Test connection
    notifier = TelegramNotifier()

    if notifier.test_connection():
        # Test morning report
        signals = SignalSummary(
            timestamp=datetime.now(),
            total_signals=5,
            bullish_signals=3,
            bearish_signals=2,
            neutral_signals=0,
            avg_ic=0.62,
            top_patterns=["Yield Curve Inversion", "VIX Spike", "M2 Money Supply Surge"],
            recommended_actions=["Reduce tech 10%", "Add defensive 5%", "Raise cash 5%"],
        )

        portfolio = PortfolioSnapshot(
            timestamp=datetime.now(),
            portfolio_value=105_000,
            daily_return_pct=0.5,
            total_return_pct=5.0,
            cash_pct=15.0,
            num_positions=8,
            sharpe_ratio=1.2,
            max_drawdown_pct=-8.5,
            health_status="HEALTHY",
            correlation=0.42,
        )

        notifier.send_morning_report(signals, portfolio)
