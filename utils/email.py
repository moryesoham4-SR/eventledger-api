import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import threading

# Environmental Configuration
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAILS_FROM_NAME = os.getenv("EMAILS_FROM_NAME", "EventLedger AI")
EMAILS_FROM_ADDRESS = os.getenv("EMAILS_FROM_ADDRESS", "") or SMTP_USER or "notifications@eventledger.internal"

def _send_email_thread(to_email: str, subject: str, html_body: str, text_body: str):
    """Executes actual SMTP email dispatch in background thread."""
    if not to_email or "@" not in to_email or "internal" in to_email:
        print(f"📧 [EMAIL SKIPPED] Internal or invalid address: {to_email}")
        return

    # Log dispatch preview
    print(f"\n📧 [EMAIL DISPATCHED VIA SMTP]")
    print(f"   To: {to_email}")
    print(f"   Subject: {subject}")

    if not SMTP_USER or not SMTP_PASSWORD:
        print("   Status: SMTP credentials not set in environment (Mock mode active).")
        print("   Config: Set SMTP_USER & SMTP_PASSWORD in .env to send real emails via Gmail/SendGrid/SES.\n")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{EMAILS_FROM_NAME} <{EMAILS_FROM_ADDRESS}>"
        msg["To"] = to_email

        msg.attach(MIMEText(text_body or "EventLedger Notification", "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAILS_FROM_ADDRESS, [to_email], msg.as_string())
        
        print("   Status: Real email delivered successfully via SMTP! ✅\n")
    except Exception as err:
        print(f"   Error delivering SMTP email to {to_email}: {err}\n")

def dispatch_email_bg(to_email: str, subject: str, html_body: str, text_body: str = ""):
    """Non-blocking background thread dispatcher."""
    t = threading.Thread(target=_send_email_thread, args=(to_email, subject, html_body, text_body))
    t.daemon = True
    t.start()

def get_base_html_template(title: str, content_html: str, action_url: str = "", action_text: str = "") -> str:
    """Returns ornate brand-matching HTML email template."""
    btn_html = ""
    if action_url and action_text:
        btn_html = f"""
        <div style="margin-top: 24px; text-align: center;">
            <a href="{action_url}" style="background-color: #6366f1; color: #ffffff; text-decoration: none; padding: 12px 24px; border-radius: 10px; font-weight: bold; font-size: 14px; display: inline-block;">
                {action_text} &rarr;
            </a>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 580px; margin: 0 auto; background-color: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 32px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
            .header {{ text-align: center; border-bottom: 1px solid #334155; padding-bottom: 20px; margin-bottom: 24px; }}
            .brand {{ font-size: 22px; font-weight: bold; color: #6366f1; letter-spacing: -0.5px; }}
            .title {{ font-size: 18px; font-weight: 700; color: #f8fafc; margin-top: 8px; }}
            .content {{ font-size: 14px; line-height: 1.6; color: #cbd5e1; }}
            .badge {{ display: inline-block; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: bold; margin-bottom: 12px; }}
            .footer {{ border-top: 1px solid #334155; margin-top: 32px; padding-top: 16px; text-align: center; font-size: 11px; color: #64748b; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="brand">⚡ EventLedger AI</div>
                <div class="title">{title}</div>
            </div>
            <div class="content">
                {content_html}
                {btn_html}
            </div>
            <div class="footer">
                This is an automated operational email from EventLedger AI Management System.<br>
                Fest & College Operations Portal &bull; All Rights Reserved.
            </div>
        </div>
    </body>
    </html>
    """

# ==================== EMAIL NOTIFICATION DISPATCHERS ====================

def send_claim_submitted_email(to_email: str, claimant_name: str, claim_amount: float, dept_name: str, item_name: str):
    subject = f"📥 New Claim Submitted: ₹{claim_amount:,.2f} for {item_name}"
    content = f"""
    <p>A new co-worker expense reimbursement claim has been submitted and requires your verification:</p>
    <div style="background-color: #0f172a; border: 1px solid #334155; padding: 16px; border-radius: 12px; margin: 16px 0;">
        <p style="margin: 4px 0;"><strong>Claimant:</strong> {claimant_name}</p>
        <p style="margin: 4px 0;"><strong>Department:</strong> {dept_name}</p>
        <p style="margin: 4px 0;"><strong>Item Description:</strong> {item_name}</p>
        <p style="margin: 4px 0; font-size: 18px; color: #a855f7;"><strong>Claim Amount: ₹{claim_amount:,.2f}</strong></p>
    </div>
    <p>Please review this claim in your Expenses dashboard to approve or decline it.</p>
    """
    html = get_base_html_template("Reimbursement Claim Submitted", content, "https://eventledger-web.vercel.app/expenses", "Review Claim in Expenses")
    dispatch_email_bg(to_email, subject, html)

def send_claim_verified_email(to_email: str, claimant_name: str, claim_amount: float, dept_name: str, item_name: str):
    subject = f"🏢 Dept Head Verified: Claim ₹{claim_amount:,.2f} for {item_name}"
    content = f"""
    <p>A reimbursement claim has been <strong>verified by Department Head</strong> and is now ready for final cash/UPI payout:</p>
    <div style="background-color: #0f172a; border: 1px solid #334155; padding: 16px; border-radius: 12px; margin: 16px 0;">
        <p style="margin: 4px 0;"><strong>Claimant:</strong> {claimant_name}</p>
        <p style="margin: 4px 0;"><strong>Department:</strong> {dept_name}</p>
        <p style="margin: 4px 0;"><strong>Item Description:</strong> {item_name}</p>
        <p style="margin: 4px 0; font-size: 18px; color: #10b981;"><strong>Payout Amount: ₹{claim_amount:,.2f}</strong></p>
    </div>
    <p>Please authorize the final payment and enter the UPI UTR / Transaction Reference # to update the Actual Expenses ledger.</p>
    """
    html = get_base_html_template("Claim Verified - Payout Pending", content, "https://eventledger-web.vercel.app/expenses", "Process Payout")
    dispatch_email_bg(to_email, subject, html)

def send_claim_paid_email(to_email: str, claimant_name: str, claim_amount: float, item_name: str, utr_reference: str):
    subject = f"💸 Claim Paid Out: ₹{claim_amount:,.2f} for {item_name}"
    content = f"""
    <p>Great news! Your reimbursement claim has been <strong>PAID OUT</strong> by Finance:</p>
    <div style="background-color: #0f172a; border: 1px solid #10b981; padding: 16px; border-radius: 12px; margin: 16px 0;">
        <p style="margin: 4px 0;"><strong>Item:</strong> {item_name}</p>
        <p style="margin: 4px 0;"><strong>Amount Paid:</strong> ₹{claim_amount:,.2f}</p>
        <p style="margin: 4px 0;"><strong>Transaction Ref / UTR:</strong> <span style="color: #38bdf8; font-family: monospace; font-weight: bold;">{utr_reference or 'N/A'}</span></p>
    </div>
    <p>The transaction has been officially recorded in the Event Ledger.</p>
    """
    html = get_base_html_template("Reimbursement Paid Out! 🎉", content, "https://eventledger-web.vercel.app/expenses", "View Expense Ledger")
    dispatch_email_bg(to_email, subject, html)

def send_team_invite_email(to_email: str, user_name: str, role_title: str, event_name: str):
    subject = f"🎉 Team Invitation: You've been assigned as {role_title} for {event_name}!"
    content = f"""
    <p>Hello <strong>{user_name}</strong>,</p>
    <p>You have been assigned to the event team for <strong>{event_name}</strong> as:</p>
    <div style="text-align: center; margin: 20px 0;">
        <span style="background-color: #6366f1; color: white; padding: 8px 18px; border-radius: 20px; font-weight: bold; font-size: 16px;">
            👑 {role_title}
        </span>
    </div>
    <p>Log in to your EventLedger account to view your assigned work tasks, department roster, and budget tools.</p>
    """
    html = get_base_html_template(f"Welcome to {event_name}!", content, "https://eventledger-web.vercel.app/login", "Log In to EventLedger")
    dispatch_email_bg(to_email, subject, html)

def send_certificates_unlocked_email(to_email: str, user_name: str, event_name: str):
    subject = f"🎓 Certificate Unlocked: Your Certificate of Appreciation for {event_name} is ready!"
    content = f"""
    <p>Dear <strong>{user_name}</strong>,</p>
    <p>The Event Lead has officially <strong>UNLOCKED Certificate Downloads</strong> for <strong>{event_name}</strong>!</p>
    <p>Thank you for your outstanding dedication and hard work. You can now preview and print your official digital <strong>Certificate of Appreciation</strong> with dual digital signatures.</p>
    """
    html = get_base_html_template("Certificate Unlocked! 📜", content, "https://eventledger-web.vercel.app/leaderboard", "Download Certificate Now")
    dispatch_email_bg(to_email, subject, html)
