"""Log categories (shared by the Discord logbook and the web dashboard; no discord.py import)."""

LOG_CATEGORIES: dict[str, tuple[str, str]] = {
    # key: (label, icon)
    "calls": ("New 911 calls", "🚨"),
    "incidents": ("New incidents & live incident cards", "📁"),
    "ai_responses": ("AI dispatch responses", "🧠"),
    "assignments": ("Unit assignments", "🚓"),
    "unit_status": ("Unit status changes", "🔁"),
    "backup": ("Backup requests", "🆘"),
    "priority": ("Priority changes", "🎚️"),
    "bolos": ("BOLO announcements", "📢"),
    "closures": ("Incident closures", "🔒"),
    "failed": ("Failed dispatch requests", "⚠️"),
    "admin": ("Administrative changes", "🛠️"),
    "ai_failures": ("AI provider failures", "🔌"),
    "radio": ("Radio traffic (announcements & voice)", "📻"),
    "erlc": ("ER:LC connection changes", "🎮"),
    # v4.0 Command Center
    "members": ("Member joins & leaves", "👋"),
    "roles": ("Member role changes", "🎭"),
    "verification": ("Roblox verification", "✅"),
    "moderation": ("Moderation actions & reports", "🛡️"),
    "applications": ("Applications", "📝"),
    "staff": ("Staff changes & leave", "👥"),
    "shifts": ("Staff shifts", "⏱️"),
    "sessions": ("Sessions", "🎮"),
    "automation": ("Automations", "⚙️"),
    "commands": ("Bot commands used", "⌨️"),
    "security": ("Security events (dashboard sign-ins, denied access)", "🔐"),
    "dashboard": ("Dashboard activity", "🖥️"),
    "errors": ("System errors", "🧯"),
    # v5.0 Platform Overhaul
    "erlc_players": ("In-game player joins & leaves", "🎮"),
    "erlc_moderation": ("In-game moderation (ER:LC)", "🚔"),
    "erlc_commands": ("In-game commands run from the dashboard", "⌨️"),
}
# Noisy categories start switched off; administrators can turn them on.
DEFAULT_ENABLED = {k: k not in {"commands", "roles", "dashboard", "erlc_players"} for k in LOG_CATEGORIES}
