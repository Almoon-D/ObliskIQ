"""
Email Notification System
=========================
Sends portfolio reports via email with HTML formatting.

Setup:
1. Use Gmail (recommended) or any SMTP server
2. For Gmail:
   - Enable 2-Factor Authentication
   - Generate App Password: https://myaccount.google.com/apppasswords
   - Use app password instead of your Gmail password
3. Set environment variables:
   EMAIL_SENDER=your_email@gmail.com
   EMAIL_PASSWORD=your_app_password  (16-character app password)
   EMAIL_RECIPIENT=your_email@gmail.com (can be same as sender)

Usage:
    from modules.notifications.email_notifier import EmailNotifier

    notifier = EmailNotifier()
    notifier.send_html_report("/path/to/report.html", "Morning Report")
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class EmailNotifier:
    """
    Send email notifications with HTML reports.

    Free tier: Gmail free account (15GB storage shared across Google services)
    Platform: Any email client
    Setup time: 10 minutes
    """

    def __init__(
        self,
        smtp_server: str = "smtp.gmail.com",
        smtp_port: int = 587,
        sender_email: Optional[str] = None,
        sender_password: Optional[str] = None,
        recipient_email: Optional[str] = None,
        enabled: bool = True,
    ):
        """
        Initialize email notifier.

        Args:
            smtp_server: SMTP server (default: Gmail)
            smtp_port: SMTP port (default: 587 for TLS)
            sender_email: Sender email (or use EMAIL_SENDER env var)
            sender_password: App password (or use EMAIL_PASSWORD env var)
            recipient_email: Recipient email (or use EMAIL_RECIPIENT env var)
            enabled: Enable/disable emails (default: True)
        """
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = sender_email or os.getenv("EMAIL_SENDER")
        self.sender_password = sender_password or os.getenv("EMAIL_PASSWORD")
        self.recipient_email = recipient_email or os.getenv("EMAIL_RECIPIENT")

        self.enabled = (
            enabled and self.sender_email and self.sender_password and self.recipient_email
        )

        if not self.enabled:
            logger.warning(
                "⚠️  Email notifications disabled. "
                "Set EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT to enable."
            )

    def send_email(
        self, subject: str, html_content: str, text_content: Optional[str] = None
    ) -> bool:
        """
        Send email with HTML content.

        Args:
            subject: Email subject
            html_content: HTML content
            text_content: Optional plain text fallback

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled:
            logger.debug(f"Email disabled, skipping: {subject}")
            return False

        try:
            # Ensure email credentials are set (already validated by self.enabled check)
            if not self.sender_email or not self.recipient_email or not self.sender_password:
                logger.error("Email credentials not configured")
                return False

            # Create message
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = self.sender_email
            message["To"] = self.recipient_email

            # Add plain text fallback (if provided)
            if text_content:
                text_part = MIMEText(text_content, "plain")
                message.attach(text_part)

            # Add HTML content
            html_part = MIMEText(html_content, "html")
            message.attach(html_part)

            # Send email
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()  # Secure connection
                server.login(self.sender_email, self.sender_password)
                server.send_message(message)

            logger.info(f"✅ Email sent: {subject}")
            return True

        except Exception as e:
            logger.error(f"❌ Email send failed: {e}")
            return False

    def send_html_report(self, html_file_path: Path, report_type: str = "Report") -> bool:
        """
        Send HTML report file via email.

        Args:
            html_file_path: Path to HTML file
            report_type: Report type ("Morning Report", "Evening Report", etc.)

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            logger.debug(f"Email disabled, skipping report: {html_file_path}")
            return False

        html_path = Path(html_file_path)

        if not html_path.exists():
            logger.error(f"❌ HTML file not found: {html_path}")
            return False

        # Read HTML content
        html_content = html_path.read_text()

        # Generate subject with timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %I:%M %p")
        subject = f"ObliskIQ {report_type} - {timestamp}"

        # Plain text fallback
        text_content = f"""
ObliskIQ Trading System - {report_type}
{timestamp}

Please view this email in an HTML-capable email client to see the full report.

Report file: {html_path.name}
        """

        return self.send_email(subject, html_content, text_content)

    def send_morning_report(self, html_file_path: Path) -> bool:
        """
        Send morning report HTML file.

        Args:
            html_file_path: Path to morning report HTML

        Returns:
            True if sent successfully
        """
        return self.send_html_report(html_file_path, "Morning Report")

    def send_evening_report(self, html_file_path: Path) -> bool:
        """
        Send evening report HTML file.

        Args:
            html_file_path: Path to evening report HTML

        Returns:
            True if sent successfully
        """
        return self.send_html_report(html_file_path, "Evening Report")

    def send_alert(self, subject: str, message: str, level: str = "INFO") -> bool:
        """
        Send plain text alert email.

        Args:
            subject: Alert subject
            message: Alert message
            level: "INFO", "WARNING", "CRITICAL"

        Returns:
            True if sent successfully
        """
        emoji_map = {
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "CRITICAL": "🚨",
        }

        emoji = emoji_map.get(level.upper(), "❗")
        full_subject = f"{emoji} {level.upper()}: {subject}"

        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{
            font-family: Arial, sans-serif;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }}
        .alert-box {{
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
        }}
        .alert-info {{ background: #e0f2fe; border-left: 4px solid #0284c7; }}
        .alert-warning {{ background: #fef3c7; border-left: 4px solid #d97706; }}
        .alert-critical {{ background: #fee2e2; border-left: 4px solid #dc2626; }}
        .timestamp {{
            color: #666;
            font-size: 14px;
            margin-top: 10px;
        }}
    </style>
</head>
<body>
    <div class="alert-box alert-{level.lower()}">
        <h2>{emoji} {level.upper()}: {subject}</h2>
        <p>{message}</p>
        <div class="timestamp">
            {datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")}
        </div>
    </div>
    <p style="color: #666; font-size: 14px; margin-top: 20px;">
        Generated by ObliskIQ Trading System v3.1.0
    </p>
</body>
</html>
        """

        return self.send_email(full_subject, html_content, message)

    def test_connection(self) -> bool:
        """
        Test email connection.

        Returns:
            True if connection successful
        """
        if not self.enabled:
            logger.error("❌ Email not configured")
            return False

        if not self.sender_email or not self.sender_password:
            logger.error("❌ Email credentials not configured")
            return False

        try:
            with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(self.sender_email, self.sender_password)

            logger.info(f"✅ Email connection successful: {self.sender_email}")

            # Send test email
            test_subject = "ObliskIQ Email Configuration Test"
            test_message = "✅ Email notifications are configured correctly!"
            self.send_alert(test_subject, test_message, "INFO")

            return True

        except Exception as e:
            logger.error(f"❌ Email connection failed: {e}")
            return False


# Example usage
if __name__ == "__main__":
    # Test connection
    notifier = EmailNotifier()

    if notifier.test_connection():
        # Test sending alert
        notifier.send_alert(
            subject="Test Alert",
            message="This is a test alert from ObliskIQ trading system.",
            level="INFO",
        )

        print("✅ Email notifications working!")
    else:
        print("❌ Email not configured. Set EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT.")
