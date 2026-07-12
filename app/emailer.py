import smtplib
from email.message import EmailMessage

from app.config import settings


def send_verification_email(to_email: str, username: str, verify_url: str) -> None:
    if not settings.smtp_host or not settings.smtp_username or not settings.smtp_password or not settings.smtp_from:
        raise RuntimeError("SMTP is not configured.")

    message = EmailMessage()
    message["Subject"] = "Verify your LuomoFile Hub account"
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message.set_content(
        "\n".join(
            [
                f"Hi {username},",
                "",
                "Please verify your LuomoFile Hub account by opening this link:",
                verify_url,
                "",
                "After verification, your account will be active immediately.",
                "",
                "If you did not request this account, you can ignore this email.",
            ]
        )
    )

    if settings.smtp_use_ssl:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
