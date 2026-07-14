# -*- coding: utf-8 -*-
"""
Garmin Connect – OAuth 2.0 PKCE integration + Health API webhook receivers.

Auth flow:
  1. GET  /api/garmin/auth/start        → returns auth_url + code_verifier
  2. User is redirected to Garmin → authorises → Garmin redirects to redirect_uri?code=...&state=...
  3. Frontend catches code+state from URL, POSTs them with code_verifier to:
     POST /api/garmin/auth/callback     → exchanges code for token, stores in DB

Webhooks (Garmin POSTs here after each device sync):
  POST /api/garmin/webhook/dailies
  POST /api/garmin/webhook/sleep
  POST /api/garmin/webhook/hrv
  POST /api/garmin/webhook/stress
"""
import os, secrets, hashlib, base64, json
from collections import deque
from datetime import datetime
from typing import Optional

import requests as _requests
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, User, UserRole, GarminData, GarminDataSource
from auth import get_current_user, require_researcher

router = APIRouter(prefix="/api/garmin", tags=["garmin-connect"])

# ── Config ─────────────────────────────────────────────────────────────────
GARMIN_CLIENT_ID     = os.environ.get("GARMIN_CLIENT_ID", "")
GARMIN_CLIENT_SECRET = os.environ.get("GARMIN_CLIENT_SECRET", "")
GARMIN_REDIRECT_URI  = "https://vahin-production.up.railway.app/"
GARMIN_AUTH_URL      = "https://connect.garmin.com/oauth2Confirm"
GARMIN_TOKEN_URL     = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
GARMIN_USER_ID_URL   = "https://apis.garmin.com/wellness-api/rest/user/id"

# In-memory store for PKCE code_verifiers keyed by state.
# State = str(user_id) + random suffix for uniqueness.
_pkce_store: dict[str, str] = {}   # state → code_verifier

# ── PKCE helpers ────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _generate_pkce() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge)."""
    code_verifier = _b64url(secrets.token_bytes(32))
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = _b64url(digest)
    return code_verifier, code_challenge

# ── Schemas ─────────────────────────────────────────────────────────────────

class CallbackPayload(BaseModel):
    code: str
    state: str
    code_verifier: str


# ── Garmin API helpers ──────────────────────────────────────────────────────

def _garmin_fetch_user_id(access_token: str) -> Optional[str]:
    """GET /wellness-api/rest/user/id — vrátí Garmin userId pro daný token."""
    try:
        r = _requests.get(
            GARMIN_USER_ID_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if r.ok:
            return str(r.json().get("userId") or "") or None
        print(f"[Garmin] user/id error {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"[Garmin] user/id exception: {exc}")
    return None


def _garmin_refresh_token(user: User, db: Session) -> Optional[str]:
    """Obnov access token pomocí refresh tokenu. Vrátí nový access token nebo None."""
    refresh = getattr(user, "garmin_token_secret", None)
    if not refresh:
        return None
    try:
        r = _requests.post(
            GARMIN_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh,
                "client_id":     GARMIN_CLIENT_ID,
                "client_secret": GARMIN_CLIENT_SECRET,
            },
            timeout=15,
        )
        if not r.ok:
            print(f"[Garmin] refresh error {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        new_token = data.get("access_token")
        if not new_token:
            return None
        user.garmin_access_token = new_token
        if data.get("refresh_token"):
            user.garmin_token_secret = data["refresh_token"]
        db.commit()
        return new_token
    except Exception as exc:
        print(f"[Garmin] refresh exception: {exc}")
        return None

# ── Auth endpoints ──────────────────────────────────────────────────────────

@router.get("/auth/start")
def garmin_auth_start(current_user: User = Depends(get_current_user)):
    """Generate PKCE parameters and return the Garmin authorisation URL."""
    if current_user.role not in (UserRole.participant, UserRole.admin):
        raise HTTPException(403, "Pouze pro účastníky")

    if not GARMIN_CLIENT_ID:
        raise HTTPException(503, "GARMIN_CLIENT_ID není nastaven na serveru")

    code_verifier, code_challenge = _generate_pkce()
    state = f"{current_user.id}-{secrets.token_urlsafe(8)}"
    _pkce_store[state] = code_verifier

    params = (
        f"client_id={GARMIN_CLIENT_ID}"
        f"&response_type=code"
        f"&state={state}"
        f"&redirect_uri={GARMIN_REDIRECT_URI}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )
    auth_url = f"{GARMIN_AUTH_URL}?{params}"

    return {
        "auth_url": auth_url,
        "code_verifier": code_verifier,
        "state": state,
    }


@router.post("/auth/callback")
def garmin_auth_callback(
    payload: CallbackPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Exchange the authorisation code for an access token and persist it."""
    if not GARMIN_CLIENT_ID or not GARMIN_CLIENT_SECRET:
        raise HTTPException(503, "Garmin credentials nejsou nastaveny na serveru")

    # Accept code_verifier from request body (client stores it in localStorage)
    code_verifier = payload.code_verifier
    # Also clean up any server-side entry for this state (belt-and-suspenders)
    _pkce_store.pop(payload.state, None)

    try:
        resp = _requests.post(
            GARMIN_TOKEN_URL,
            data={
                "grant_type":    "authorization_code",
                "code":          payload.code,
                "redirect_uri":  GARMIN_REDIRECT_URI,
                "client_id":     GARMIN_CLIENT_ID,
                "client_secret": GARMIN_CLIENT_SECRET,
                "code_verifier": code_verifier,
            },
            timeout=15,
        )
    except Exception as exc:
        raise HTTPException(502, f"Chyba komunikace s Garmin: {exc}")

    if not resp.ok:
        raise HTTPException(400, f"Garmin token error {resp.status_code}: {resp.text[:300]}")

    token_data = resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(400, "Garmin nevrátil access_token")

    # Persist token (and userId if provided)
    user = db.query(User).filter(User.id == current_user.id).first()
    user.garmin_access_token = access_token
    user.garmin_token_secret = token_data.get("refresh_token") or token_data.get("token_secret")
    garmin_uid = token_data.get("userId") or token_data.get("user_id")
    if not garmin_uid:
        # OAuth2 token response neobsahuje userId – dotáhni ho z Wellness API.
        # Bez něj nedokážeme spárovat webhooky (ty posílají userId, ne token).
        garmin_uid = _garmin_fetch_user_id(access_token)
    if garmin_uid:
        user.garmin_user_id = str(garmin_uid)
    db.commit()

    return {"ok": True, "garmin_user_id": user.garmin_user_id}


@router.get("/status")
def garmin_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return whether the current user has a Garmin connection."""
    user = db.query(User).filter(User.id == current_user.id).first()
    connected = bool(getattr(user, "garmin_access_token", None))
    garmin_uid = getattr(user, "garmin_user_id", None)

    # Find the most recent garmin_data record for a rough "last sync" timestamp
    last_record = (
        db.query(GarminData)
        .filter(GarminData.user_id == current_user.id,
                GarminData.source == GarminDataSource.api)
        .order_by(GarminData.date.desc())
        .first()
    )
    last_sync = last_record.date.strftime("%Y-%m-%d") if last_record else None

    return {"connected": connected, "garmin_user_id": garmin_uid, "last_sync": last_sync}


@router.delete("/disconnect")
def garmin_disconnect(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear Garmin credentials for the current user."""
    user = db.query(User).filter(User.id == current_user.id).first()
    user.garmin_access_token = None
    user.garmin_token_secret = None
    user.garmin_user_id = None
    db.commit()
    return {"ok": True}

@router.get("/admin/status/{user_id}", dependencies=[Depends(require_researcher)])
def garmin_admin_status(user_id: int, db: Session = Depends(get_db)):
    """Admin: diagnostika Garmin připojení konkrétního účastníka."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Účastník nenalezen")

    token = getattr(user, "garmin_access_token", None)
    token_preview = (token[:8] + "…") if token else None

    records = (
        db.query(GarminData)
        .filter(GarminData.user_id == user_id)
        .order_by(GarminData.date.desc())
        .limit(5)
        .all()
    )
    api_records = [r for r in records if r.source == GarminDataSource.api]
    total_count = db.query(GarminData).filter(GarminData.user_id == user_id).count()

    return {
        "user_id": user_id,
        "participant_code": user.participant_code,
        "garmin_connected": bool(token),
        "garmin_access_token_preview": token_preview,
        "garmin_user_id": getattr(user, "garmin_user_id", None),
        "garmin_refresh_token_set": bool(getattr(user, "garmin_token_secret", None)),
        "total_garmin_records": total_count,
        "recent_records": [
            {"date": r.date.strftime("%Y-%m-%d"), "source": r.source,
             "hrv": r.hrv_rmssd, "sleep_score": r.sleep_score, "steps": r.steps}
            for r in records
        ],
        "api_records_count": len(api_records),
    }


@router.post("/admin/fetch-uid/{user_id}", dependencies=[Depends(require_researcher)])
def garmin_admin_fetch_uid(user_id: int, db: Session = Depends(get_db)):
    """Admin: zpětně dotáhni Garmin userId přes uložený token (s obnovou, pokud expiroval)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Účastník nenalezen")
    token = getattr(user, "garmin_access_token", None)
    if not token:
        raise HTTPException(400, "Účastník nemá uložený Garmin token – proveďte párování")

    uid = _garmin_fetch_user_id(token)
    if not uid:
        # Access token nejspíš expiroval → zkus obnovit refresh tokenem
        new_token = _garmin_refresh_token(user, db)
        if new_token:
            uid = _garmin_fetch_user_id(new_token)
    if not uid:
        raise HTTPException(502, "Garmin userId se nepodařilo získat – token je neplatný, proveďte nové párování")

    user.garmin_user_id = uid
    db.commit()
    return {"ok": True, "garmin_user_id": uid}


@router.post("/admin/relink/{user_id}", dependencies=[Depends(require_researcher)])
def garmin_admin_relink(user_id: int, db: Session = Depends(get_db)):
    """Admin: smaž Garmin token účastníka, aby mohl provést nové párování."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Účastník nenalezen")
    user.garmin_access_token = None
    user.garmin_token_secret = None
    db.commit()
    return {"ok": True, "message": "Garmin token smazán – účastník musí provést nové párování"}


# ── Webhook helpers ──────────────────────────────────────────────────────────

# Ring buffer posledních příchozích webhooků pro diagnostiku z admin UI
# (Railway logy nejsou z aplikace dostupné). Přežívá jen do restartu procesu.
_webhook_log: deque = deque(maxlen=200)


def _log_webhook(endpoint: str, body: dict, results: list):
    entry = {
        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "endpoint": endpoint,
        "payload_keys": {k: (len(v) if isinstance(v, list) else type(v).__name__)
                         for k, v in body.items()} if isinstance(body, dict) else str(type(body)),
        "results": results,
        "raw_sample": json.dumps(body, default=str)[:2000],
    }
    _webhook_log.append(entry)
    print(f"[Garmin webhook/{endpoint}] {entry['payload_keys']} → {results}")


def _find_user_for_item(item: dict, db: Session) -> Optional[User]:
    """Najdi účastníka pro webhook záznam.

    OAuth2 webhooky identifikují uživatele přes `userId`; `userAccessToken`
    (OAuth1 styl) navíc expiruje a rotuje, takže slouží jen jako fallback.
    """
    uid = item.get("userId")
    if uid:
        user = db.query(User).filter(User.garmin_user_id == str(uid)).first()
        if user:
            return user
    token = item.get("userAccessToken")
    if token:
        return db.query(User).filter(User.garmin_access_token == token).first()
    return None


def _upsert_garmin(user_id: int, date_val: datetime, updates: dict, db: Session):
    """Insert or update a GarminData row for (user_id, date).

    Klíč 'meta' se slučuje s existujícím JSON (časy min/max hodnot)."""
    record = (
        db.query(GarminData)
        .filter(GarminData.user_id == user_id, GarminData.date == date_val)
        .first()
    )
    if not record:
        record = GarminData(
            user_id=user_id,
            date=date_val,
            source=GarminDataSource.api,
        )
        db.add(record)
    else:
        record.source = GarminDataSource.api

    meta_updates = updates.pop("meta", None)
    for field, value in updates.items():
        if value is not None:
            setattr(record, field, value)
    if meta_updates:
        try:
            meta = json.loads(record.meta) if record.meta else {}
        except Exception:
            meta = {}
        meta.update({k: v for k, v in meta_updates.items() if v is not None})
        record.meta = json.dumps(meta, ensure_ascii=False)
    db.commit()


def _local_hhmm(unix_seconds):
    """Unix čas → 'HH:MM' v českém čase (pro tooltip u min/max hodnot)."""
    try:
        from tzutil import PRAGUE
        from datetime import timezone as _tz
        return datetime.fromtimestamp(unix_seconds, PRAGUE or _tz.utc).strftime("%H:%M")
    except Exception:
        return None


def _series_extremes(item: dict, key: str):
    """Z časové řady {offsetSekundy: hodnota} vrať (min, minČas, max, maxČas).

    Záporné hodnoty (senzor mimo zápěstí) se ignorují."""
    series = item.get(key)
    start = item.get("startTimeInSeconds")
    if not isinstance(series, dict) or not series or start is None:
        return None
    points = [(int(off), v) for off, v in series.items()
              if isinstance(v, (int, float)) and v >= 0]
    if not points:
        return None
    min_off, min_v = min(points, key=lambda p: p[1])
    max_off, max_v = max(points, key=lambda p: p[1])
    return (min_v, _local_hhmm(start + min_off), max_v, _local_hhmm(start + max_off))


def _parse_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None


def _store_garmin_user_id(user: User, item: dict, db: Session):
    uid = item.get("userId")
    if uid and not user.garmin_user_id:
        user.garmin_user_id = str(uid)
        db.commit()


def _fetch_callback_data(user: User, url: str, db: Session) -> list:
    """Ping notifikace neobsahuje data – stáhni je z callbackURL (Bearer token,
    při 401 obnov token refresh tokenem a zkus znovu)."""
    token = getattr(user, "garmin_access_token", None)
    if not token:
        return []
    for attempt in (1, 2):
        try:
            r = _requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        except Exception as exc:
            print(f"[Garmin ping] fetch exception: {exc}")
            return []
        if r.status_code == 401 and attempt == 1:
            token = _garmin_refresh_token(user, db)
            if not token:
                print(f"[Garmin ping] token expiroval a refresh selhal ({user.participant_code})")
                return []
            continue
        if not r.ok:
            print(f"[Garmin ping] fetch error {r.status_code}: {r.text[:200]}")
            return []
        try:
            data = r.json()
        except Exception:
            return []
        # API vrací buď rovnou seznam, nebo objekt {klic: [...]}
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []
    return []


def _iter_summaries(body: dict, key: str, endpoint: str, db: Session, results: list):
    """Projdi webhook payload a vydávej (user, summary) dvojice.

    Zvládá push režim (summary obsahuje data) i ping režim (jen callbackURL,
    data se musí stáhnout zvlášť). Záznamy bere z očekávaného klíče, a pokud
    tam nejsou, z libovolného klíče se seznamem – Garmin názvy klíčů
    v notifikacích ne vždy odpovídají názvu summary typu (např. HRV).
    `results` plní popisy zpracování pro webhook log."""
    if not isinstance(body, dict):
        results.append("payload není objekt")
        return
    items = body.get(key)
    if not isinstance(items, list) or not items:
        # fallback: vezmi všechny list-klíče (jiný název než čekáme)
        items = []
        for k, v in body.items():
            if isinstance(v, list):
                items.extend(v)
                results.append(f"klíč '{k}' místo '{key}' ({len(v)} záznamů)")
    if not items:
        results.append("žádné záznamy v payloadu")
    for item in items:
        if not isinstance(item, dict):
            continue
        user = _find_user_for_item(item, db)
        if not user:
            results.append(f"UNKNOWN userId={item.get('userId')} – žádný účastník nenalezen")
            continue
        _store_garmin_user_id(user, item, db)
        if item.get("callbackURL") and not item.get("calendarDate"):
            # ping notifikace – stáhni skutečná data
            fetched = _fetch_callback_data(user, item["callbackURL"], db)
            results.append(f"ping pro {user.participant_code} → staženo {len(fetched)} záznamů")
            for real in fetched:
                if isinstance(real, dict):
                    yield user, real
        else:
            results.append(f"push data pro {user.participant_code} ({item.get('calendarDate')})")
            yield user, item

# ── Webhook endpoints ────────────────────────────────────────────────────────

def _apply_daily(user: User, item: dict, db: Session) -> bool:
    date_val = _parse_date(item.get("calendarDate", ""))
    if not date_val:
        return False
    updates = {
        "steps":           item.get("steps"),
        "resting_hr":      item.get("restingHeartRateInBeatsPerMinute"),
        "max_hr":          item.get("maxHeartRateInBeatsPerMinute"),
        "spo2_avg":        item.get("averageSpO2"),
        "respiration_avg": item.get("averageRespirationValue"),
        "stress_avg":      item.get("averageStressLevel"),
        "calories_active": item.get("activeKilocalories"),
        # bodyBatteryCharged/DrainedValue = kolik se nabilo/vybilo, NE min/max
        # → skutečné min/max se počítají z časové řady ve stress webhoooku
    }
    # Čas maxima TF z časové řady tepů (offset sekund → BPM)
    hr = _series_extremes(item, "timeOffsetHeartRateSamples")
    if hr:
        _min_v, _min_t, max_v, max_t = hr
        if updates["max_hr"] is None:
            updates["max_hr"] = int(max_v)
        updates["meta"] = {"max_hr_time": max_t}
    mod = item.get("moderateIntensityDurationInSeconds") or 0
    vig = item.get("vigorousIntensityDurationInSeconds") or 0
    if mod or vig:
        updates["active_minutes"] = int((mod + vig) // 60)
    _upsert_garmin(user.id, date_val, updates, db)
    return True


def _apply_sleep(user: User, item: dict, db: Session) -> bool:
    date_val = _parse_date(item.get("calendarDate", ""))
    if not date_val:
        return False
    duration_s = item.get("durationInSeconds")
    deep_s     = item.get("deepSleepDurationInSeconds")
    rem_s      = item.get("remSleepInSeconds")
    # overallSleepScore chodí buď jako číslo, nebo objekt {"value": 78, ...}
    score = item.get("overallSleepScore")
    if isinstance(score, dict):
        score = score.get("value")
    updates = {
        "sleep_hours":    round(duration_s / 3600, 2) if duration_s else None,
        "sleep_score":    score,
        "deep_sleep_min": int(deep_s // 60) if deep_s else None,
        "rem_sleep_min":  int(rem_s // 60) if rem_s else None,
        "spo2_avg":       item.get("averageSpO2Value"),
        "respiration_avg": item.get("averageRespirationValue"),
    }
    _upsert_garmin(user.id, date_val, updates, db)
    return True


def _apply_hrv(user: User, item: dict, db: Session) -> bool:
    date_val = _parse_date(item.get("calendarDate", ""))
    if not date_val:
        return False
    updates = {
        "hrv_rmssd":      item.get("lastNightAvg") or item.get("lastNight"),
        "hrv_weekly_avg": item.get("weeklyAvg") or item.get("weeklyAverage"),
    }
    _upsert_garmin(user.id, date_val, updates, db)
    return True


def _apply_stress(user: User, item: dict, db: Session) -> bool:
    date_val = _parse_date(item.get("calendarDate", ""))
    if not date_val:
        return False
    updates = {
        "stress_avg":        item.get("averageStressLevel"),
        "body_battery_high": item.get("bodyBatteryHighestValue"),
        "body_battery_low":  item.get("bodyBatteryLowestValue"),
    }
    meta = {}
    # Body battery min/max + časy z časové řady (explicitní pole Garmin
    # posílá jen někdy, řada bývá spolehlivější)
    bb = _series_extremes(item, "timeOffsetBodyBatteryValues")
    if bb:
        low_v, low_t, high_v, high_t = bb
        if updates["body_battery_low"] is None:
            updates["body_battery_low"] = int(low_v)
        if updates["body_battery_high"] is None:
            updates["body_battery_high"] = int(high_v)
        meta["bb_low_time"] = low_t
        meta["bb_high_time"] = high_t
    # Průměrný stres dopočítej z řady, když chybí explicitní pole
    if updates["stress_avg"] is None:
        series = item.get("timeOffsetStressLevelValues")
        if isinstance(series, dict):
            vals = [v for v in series.values() if isinstance(v, (int, float)) and v >= 0]
            if vals:
                updates["stress_avg"] = int(round(sum(vals) / len(vals)))
    if meta:
        updates["meta"] = meta
    _upsert_garmin(user.id, date_val, updates, db)
    return True


@router.get("/webhook/{summary_type}")
def webhook_health_check(summary_type: str):
    """GET na webhook URL (prohlížeč, verifikace Garmin portálu) → 200 OK."""
    return {"ok": True, "endpoint": summary_type, "info": "Garmin sem posílá data přes POST"}


async def _handle_webhook(request: Request, key: str, endpoint: str, apply_fn, db: Session):
    try:
        body = await request.json()
    except Exception:
        _log_webhook(endpoint, {}, ["tělo požadavku není JSON"])
        return {"ok": True}
    results: list = []
    saved = 0
    for user, item in _iter_summaries(body, key, endpoint, db, results):
        if apply_fn(user, item, db):
            saved += 1
        else:
            results.append(f"záznam bez calendarDate přeskočen (klíče: {list(item.keys())[:8]})")
    results.append(f"uloženo {saved} záznamů")
    _log_webhook(endpoint, body, results)
    return {"ok": True}


@router.post("/webhook/dailies")
async def webhook_dailies(request: Request, db: Session = Depends(get_db)):
    """Receive daily summary push from Garmin Health API."""
    return await _handle_webhook(request, "dailies", "dailies", _apply_daily, db)


@router.post("/webhook/sleep")
async def webhook_sleep(request: Request, db: Session = Depends(get_db)):
    """Receive sleep summary push from Garmin Health API."""
    return await _handle_webhook(request, "sleeps", "sleep", _apply_sleep, db)


@router.post("/webhook/hrv")
async def webhook_hrv(request: Request, db: Session = Depends(get_db)):
    """Receive HRV summary push from Garmin Health API."""
    return await _handle_webhook(request, "hrv", "hrv", _apply_hrv, db)


@router.post("/webhook/stress")
async def webhook_stress(request: Request, db: Session = Depends(get_db)):
    """Receive stress detail push from Garmin Health API."""
    return await _handle_webhook(request, "stressDetails", "stress", _apply_stress, db)


@router.get("/admin/webhook-log", dependencies=[Depends(require_researcher)])
def garmin_admin_webhook_log(limit: int = 50):
    """Admin: posledních N příchozích Garmin webhooků (diagnostika bez Railway logů)."""
    limit = max(1, min(limit, 200))
    entries = list(_webhook_log)[-limit:]
    entries.reverse()
    return {"count": len(entries), "entries": entries}


# ── Aktivní stažení dat z Garmin API (pull / backfill) ──────────────────────

_PULL_TYPES = [
    ("dailies",       "https://apis.garmin.com/wellness-api/rest/dailies",       _apply_daily),
    ("sleeps",        "https://apis.garmin.com/wellness-api/rest/sleeps",        _apply_sleep),
    ("hrv",           "https://apis.garmin.com/wellness-api/rest/hrv",           _apply_hrv),
    ("stressDetails", "https://apis.garmin.com/wellness-api/rest/stressDetails", _apply_stress),
]

def _request_backfill(user: User, db: Session, days: int = 7) -> dict:
    """Vyžádej u Garminu backfill posledních N dní (data pak přijdou webhooky).

    Vrací {"ok", "token_valid", "backfill": {...}, "errors": [...]}."""
    import time as _time
    token = getattr(user, "garmin_access_token", None)
    if not token:
        return {"ok": False, "token_valid": False, "backfill": {},
                "errors": ["Účastník nemá uložený Garmin token"]}

    # Ověř platnost tokenu; při 401 zkus refresh
    token_valid = False
    token_check = "?"
    try:
        r = _requests.get(GARMIN_USER_ID_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if r.status_code == 401:
            token = _garmin_refresh_token(user, db)
            if token:
                r = _requests.get(GARMIN_USER_ID_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        token_valid = bool(token) and r.ok
        token_check = f"{r.status_code}: {r.text[:150]}" if not r.ok else "OK"
    except Exception as exc:
        token_check = f"výjimka: {exc}"

    if not token_valid:
        return {"ok": False, "token_valid": False, "token_check": token_check,
                "backfill": {}, "errors": ["Token je neplatný a refresh selhal – nutné nové párování"]}

    # Aplikace je u Garminu v "evented" režimu → přímý pull vrací
    # InvalidPullTokenException. Místo toho Backfill API: Garmin data
    # pošle asynchronně přes webhooky, které už umíme zpracovat.
    days = max(1, min(days, 30))
    now = int(_time.time())
    start = now - days * 86400
    stats = {}
    errors = []

    for key, _url, _fn in _PULL_TYPES:
        bf_url = (f"https://apis.garmin.com/wellness-api/rest/backfill/{key}"
                  f"?summaryStartTimeInSeconds={start}&summaryEndTimeInSeconds={now}")
        try:
            r = _requests.get(bf_url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        except Exception as exc:
            errors.append(f"{key}: výjimka {exc}")
            stats[key] = "chyba"
            continue
        if r.status_code in (200, 202):
            stats[key] = "vyžádáno"
        elif r.status_code == 409:
            stats[key] = "už vyžádáno dříve"   # duplicitní backfill okna Garmin odmítá, není to chyba
        else:
            stats[key] = f"HTTP {r.status_code}"
            if len(errors) < 8:
                errors.append(f"{key}: HTTP {r.status_code} {r.text[:150]}")

    errors = list(dict.fromkeys(errors))
    return {"ok": True, "token_valid": True, "days": days, "backfill": stats, "errors": errors}


@router.post("/admin/pull/{user_id}", dependencies=[Depends(require_researcher)])
def garmin_admin_pull(user_id: int, days: int = 7, db: Session = Depends(get_db)):
    """Admin: vyžádej backfill dat z Garmin API za posledních N dní (max 30)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Účastník nenalezen")
    if not getattr(user, "garmin_access_token", None):
        raise HTTPException(400, "Účastník nemá uložený Garmin token")
    return _request_backfill(user, db, days)


def garmin_gap_backfill(db_session_factory):
    """Automatická samoopravná kontrola (spouští APScheduler à la noční sync):
    když u propojeného účastníka chybí data za poslední dny (výpadek webhooků,
    restart serveru…), vyžádá si je backfillem sama. Výsledek se zapisuje do
    webhook logu (admin → Webhook log)."""
    from datetime import timedelta
    from tzutil import now_prague
    db = db_session_factory()
    try:
        users = db.query(User).filter(User.garmin_access_token.isnot(None)).all()
        for user in users:
            gaps = []
            for d in range(1, 4):   # včerejšek až 3 dny zpět
                day = now_prague().date() - timedelta(days=d)
                day_dt = datetime(day.year, day.month, day.day)
                rec = (db.query(GarminData)
                       .filter(GarminData.user_id == user.id, GarminData.date == day_dt)
                       .first())
                if not rec or rec.sleep_hours is None or rec.hrv_rmssd is None or not rec.steps:
                    gaps.append(day.strftime("%d.%m."))
            if not gaps:
                continue
            res = _request_backfill(user, db, days=4)
            _log_webhook("auto-backfill", {"účastník": user.participant_code, "chybějící dny": gaps},
                         [f"backfill: {res.get('backfill')}", *res.get("errors", [])])
    except Exception as exc:
        print(f"[garmin_gap_backfill] ERROR: {exc}")
    finally:
        db.close()
