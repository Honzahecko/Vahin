# -*- coding: utf-8 -*-
"""
Časové pomůcky. DB ukládá naivní UTC (datetime.utcnow), ale klienti potřebují
buď ISO s označením zóny (prohlížeč si převede na lokální čas sám), nebo
rovnou český čas (exporty pro výzkumníky).
"""
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    PRAGUE = ZoneInfo("Europe/Prague")
except Exception:
    PRAGUE = None


def utc_iso(dt):
    """Naivní UTC datetime → ISO 8601 s '+00:00'. None → None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def prague_str(dt):
    """Naivní UTC datetime → 'YYYY-MM-DD HH:MM' v českém čase (pro exporty)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PRAGUE).strftime("%Y-%m-%d %H:%M") if PRAGUE else dt.isoformat()


def now_prague():
    """Aktuální čas v Praze – pro výpočty dne studie konzistentní s plánovačem."""
    return datetime.now(PRAGUE) if PRAGUE else datetime.now(timezone.utc)
