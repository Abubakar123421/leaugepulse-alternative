from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, PageBreak, Spacer,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "Discord_League_Bot_Project_Plan.pdf"

NAVY = colors.HexColor("#17223B")
BLUE = colors.HexColor("#2F6FED")
MUTED = colors.HexColor("#5F6B7A")
PALE = colors.HexColor("#EDF3FF")


def footer(canvas, doc):
    canvas.saveState()
    width, _ = letter
    canvas.setStrokeColor(colors.HexColor("#D8DEE9"))
    canvas.line(0.7 * inch, 0.55 * inch, width - 0.7 * inch, 0.55 * inch)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.7 * inch, 0.34 * inch, "Discord League Bot - Project Plan")
    canvas.drawRightString(width - 0.7 * inch, 0.34 * inch, f"Page {doc.page}")
    canvas.restoreState()


def build() -> Path:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="CoverTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=28, leading=34, textColor=NAVY, alignment=TA_CENTER, spaceAfter=18,
    ))
    styles.add(ParagraphStyle(
        name="CoverSub", parent=styles["Normal"], fontSize=13, leading=20,
        textColor=MUTED, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="Section", parent=styles["Heading1"], fontName="Helvetica-Bold",
        fontSize=18, leading=23, textColor=NAVY, spaceBefore=4, spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="Subsection", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=16, textColor=BLUE, spaceBefore=9, spaceAfter=5,
    ))
    styles.add(ParagraphStyle(
        name="BodyPlan", parent=styles["BodyText"], fontSize=10, leading=15,
        textColor=colors.HexColor("#283342"), spaceAfter=7,
    ))
    styles.add(ParagraphStyle(
        name="BulletPlan", parent=styles["BodyText"], fontSize=10, leading=14,
        leftIndent=15, firstLineIndent=-8, bulletIndent=4, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="Callout", parent=styles["BodyText"], fontSize=10, leading=15,
        textColor=NAVY, backColor=PALE, borderColor=colors.HexColor("#C8D8FF"),
        borderWidth=0.7, borderPadding=10, spaceBefore=8, spaceAfter=12,
    ))
    doc = BaseDocTemplate(
        str(OUTPUT), pagesize=letter, leftMargin=0.72 * inch, rightMargin=0.72 * inch,
        topMargin=0.68 * inch, bottomMargin=0.75 * inch,
        title="Discord League Bot Project Plan", author="League Bot Development",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates(PageTemplate(id="standard", frames=frame, onPage=footer))
    story = [
        Spacer(1, 1.55 * inch),
        Paragraph("Discord League Bot", styles["CoverTitle"]),
        Paragraph("Project Plan and Delivery Overview", styles["CoverSub"]),
        Spacer(1, 0.32 * inch),
        Paragraph(
            "A reusable league-management bot for Madden and College Football, "
            "built to keep each Discord server independent while giving players and "
            "commissioners one clear weekly workflow.",
            styles["Callout"],
        ),
        Spacer(1, 0.32 * inch),
        Paragraph("Prepared for League Operations", styles["CoverSub"]),
        Paragraph("Current delivery scope - July 2026", styles["CoverSub"]),
        PageBreak(),
    ]

    def section(title: str, intro: str, bullets: list[str]) -> None:
        story.append(Paragraph(title, styles["Section"]))
        story.append(Paragraph(intro, styles["BodyPlan"]))
        for bullet in bullets:
            story.append(Paragraph(bullet, styles["BulletPlan"], bulletText="-"))

    section(
        "1. Project outcome",
        "One Discord bot application will support separate Madden and College Football "
        "servers. League names, teams, profiles, schedules, channels, reminders, and "
        "commissioner decisions remain separate for every server.",
        [
            "Reusable for Madden 26, Madden 27 and later seasons.",
            "Reusable for College Football 27 and later seasons.",
            "The operator creates the College Football Discord server, but he does not need to "
            "create its league channels. The setup command creates or connects them.",
            "The complete source, database, configuration template, backups, and transfer "
            "instructions are portable to another host.",
        ],
    )
    section(
        "2. Setup, settings, and help",
        "Commissioners can configure each league without editing source files.",
        [
            "/setup creates or links the league category and required channels.",
            "Each server has its own league name, game, season, week, timezone, advance "
            "day and time, channels, commissioner role, reminders, and module settings.",
            "/settings displays and updates the active league configuration.",
            "/help provides a private interactive help center with categories, command "
            "details, navigation buttons, examples, and permission-aware visibility.",
        ],
    )
    section(
        "3. Members, teams, and streams",
        "Player profiles connect Discord members to league teams and approved stream links.",
        [
            "/register records a requested team plus optional Twitch and YouTube links.",
            "A commissioner approves each team claim; the same member may have different "
            "teams in different Discord servers.",
            "Approved stream links appear in matchup posts.",
            "Twitch live alerts post automatically when developer credentials are configured. "
            "YouTube links are shown in profiles and matchups, with alerts available when "
            "the required channel details and API key are configured.",
            "Team logos can be added; team names remain as the fallback.",
        ],
    )
    story.append(PageBreak())
    section(
        "4. Matchup workflow",
        "Every imported game receives a private thread that keeps scheduling and league "
        "decisions in one place.",
        [
            "The thread shows teams, records, player mentions, advance deadline and timezone, "
            "approved Twitch and YouTube links, and the current status.",
            "Schedule Game records a proposed date and time.",
            "Mark Complete collects both scores and sends the result for commissioner approval.",
            "Request Commissioner alerts the configured role and includes a cooldown.",
            "Report Issue collects details and flags the matchup for staff review.",
            "Complete games, force wins, and fair sims stop receiving reminders.",
        ],
    )
    section(
        "5. Commissioner dashboard",
        "/week shows every matchup for the selected or current week and provides the ID "
        "needed for league decisions.",
        [
            "Statuses include Complete, Scheduled, Waiting, Overdue, and Issue Reported.",
            "Commissioner outcomes include approval, home or away force win, fair sim, "
            "reminder handling, result review, issue review, and week advancement.",
            "Important decisions require confirmation and are written to an audit log.",
            "Week advancement is blocked until every unresolved matchup is handled.",
        ],
    )
    section(
        "6. Smart reminders",
        "Reminders help players schedule early without repeating messages after a restart.",
        [
            "Matchups are available immediately after the commissioner confirms an import "
            "and opens the week dashboard.",
            "A no-response reminder is available after 24 hours.",
            "Deadline reminders are available 48, 24, and 6 hours before advance.",
            "Each milestone is stored once per matchup and can be enabled or disabled.",
        ],
    )
    story.append(PageBreak())
    section(
        "7. Schedule imports",
        "Imports are checked before anything is created.",
        [
            "Madden accepts original NeonSportz Teams/Standings and Games CSV-style columns. "
            "Original client samples are still required to confirm any extra column aliases.",
            "College Football uses the included CSV template.",
            "The bot displays a preview and validation errors before confirmation.",
            "Corrected re-imports update the existing game instead of creating duplicates.",
            "New seasons reuse the same bot and keep season-sensitive data separate.",
        ],
    )
    section(
        "8. League modules",
        "The current delivery includes the practical content tools requested for both games.",
        [
            "Madden: trade tracking, trade block, and Open Rosters/Open Teams.",
            "College Football: transfer tracking and Open Rosters/Open Teams.",
            "Both: announcements, Game of the Week, Players of the Week, approved stream "
            "links, live alerts, and per-module controls.",
            "Trade and transfer tracking covers information entered or imported into this "
            "bot. It does not promise undocumented EA or NeonSportz access.",
        ],
    )
    section(
        "9. Delivery and hosting",
        "The project is designed for bot-hosting.net and future transfer.",
        [
            "Startup file, package list, configuration template, persistent data folder, "
            "backup command, operator guide, and hosting-transfer guide are included.",
            "Live Discord tokens, passwords, and streaming credentials are excluded from "
            "the source package and documentation.",
            "The SQLite database can be backed up, downloaded, restored, and transferred "
            "with the source.",
        ],
    )
    story.append(PageBreak())
    section(
        "10. Information still needed",
        "The software can be prepared without live secrets, but final league setup needs "
        "the following details.",
        [
            "College Football server access and permission to install the bot.",
            "League names, timezone, default advance schedule, season, and current week.",
            "Commissioner accounts or role and team assignments.",
            "Original, unedited NeonSportz Teams/Standings and Games CSV samples.",
            "College Football assignments and schedule when ready.",
            "Team logo files and any preferred channel names.",
            "Discord application token, hosting access, optional Twitch credentials, "
            "optional YouTube API key, and final bot branding.",
        ],
    )
    section(
        "11. Future expansion - not in this delivery",
        "The design allows these ideas to be added later, but they are intentionally outside "
        "the current paid scope.",
        [
            "Power rankings, storyline posts, recruiting posts, and playoff picture.",
            "League history, expanded statistics and awards, and automatic game recaps.",
            "Any feature that depends on an undocumented third-party API.",
        ],
    )
    story.append(Paragraph(
        "<b>Delivery principle:</b> the bot will automate the confirmed league workflow "
        "without exposing credentials or promising data access that has not been verified.",
        styles["Callout"],
    ))
    doc.build(story)
    return OUTPUT


if __name__ == "__main__":
    print(build())
