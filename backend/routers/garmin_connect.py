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
import os, secrets, hashlib, base64
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
    """Insert or update a GarminData row for (user_id, date)."""
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

    for field, value in updates.items():
        if value is not None:
            setattr(record, field, value)
    db.commit()


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

# ── Webhook endpoints ────────────────────────────────────────────────────────

@router.post("/webhook/dailies")
async def webhook_dailies(request: Request, db: Session = Depends(get_db)):
    """Receive daily summary push from Garmin Health API."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    items = body.get("dailies", [])
    if not isinstance(items, list):
        items = []

    for item in items:
        user = _find_user_for_item(item, db)
        if not user:
            print(f"[Garmin webhook/dailies] UNKNOWN userId={item.get('userId')} – žádný účastník nenalezen")
            continue
        print(f"[Garmin webhook/dailies] Data pro {user.participant_code} ({item.get('calendarDate')})")
        _store_garmin_user_id(user, item, db)

        date_val = _parse_date(item.get("calendarDate", ""))
        if not date_val:
            continue

        updates = {
            "steps":           item.get("steps"),
            "resting_hr":      item.get("restingHeartRateInBeatsPerMinute"),
            "max_hr":          item.get("maxHeartRateInBeatsPerMinute"),
            "spo2_avg":        item.get("averageSpO2"),
            "respiration_avg": item.get("averageRespirationValue"),
            "stress_avg":      item.get("averageStressLevel"),
            "calories_active": item.get("activeKilocalories"),
            # bodyBatteryCharged/DrainedValue = kolik se nabilo/vybilo, NE min/max
            # → skutečné min/max posílá stress webhook (Highest/LowestValue)
        }
        mod = item.get("moderateIntensityDurationInSeconds") or 0
        vig = item.get("vigorousIntensityDurationInSeconds") or 0
        if mod or vig:
            updates["active_minutes"] = int((mod + vig) // 60)
        _upsert_garmin(user.id, date_val, updates, db)

    return {"ok": True}


@router.post("/webhook/sleep")
async def webhook_sleep(request: Request, db: Session = Depends(get_db)):
    """Receive sleep summary push from Garmin Health API."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    items = body.get("sleeps", [])
    if not isinstance(items, list):
        items = []

    for item in items:
        user = _find_user_for_item(item, db)
        if not user:
            print(f"[Garmin webhook/sleep] UNKNOWN userId={item.get('userId')} – žádný účastník nenalezen")
            continue
        print(f"[Garmin webhook/sleep] Data pro {user.participant_code} ({item.get('calendarDate')})")
        _store_garmin_user_id(user, item, db)

        date_val = _parse_date(item.get("calendarDate", ""))
        if not date_val:
            continue

        duration_s = item.get("durationInSeconds")
        deep_s     = item.get("deepSleepDurationInSeconds")
        rem_s      = item.get("remSleepInSeconds")

        updates = {
            "sleep_hours":    round(duration_s / 3600, 2) if duration_s else None,
            "sleep_score":    item.get("overallSleepScore"),
            "deep_sleep_min": int(deep_s // 60) if deep_s else None,
            "rem_sleep_min":  int(rem_s // 60) if rem_s else None,
            "spo2_avg":       item.get("averageSpO2Value"),
            "respiration_avg": item.get("averageRespirationValue"),
        }
        _upsert_garmin(user.id, date_val, updates, db)

    return {"ok": True}


@router.post("/webhook/hrv")
async def webhook_hrv(request: Request, db: Session = Depends(get_db)):
    """Receive HRV summary push from Garmin Health API."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    items = body.get("hrv", [])
    if not isinstance(items, list):
        items = []

    for item in items:
        user = _find_user_for_item(item, db)
        if not user:
            print(f"[Garmin webhook/hrv] UNKNOWN userId={item.get('userId')} – žádný účastník nenalezen")
            continue
        print(f"[Garmin webhook/hrv] Data pro {user.participant_code} ({item.get('calendarDate')})")
        _store_garmin_user_id(user, item, db)

        date_val = _parse_date(item.get("calendarDate", ""))
        if not date_val:
            continue

        updates = {
            "hrv_rmssd":      item.get("lastNight"),
            "hrv_weekly_avg": item.get("weeklyAverage"),
        }
        _upsert_garmin(user.id, date_val, updates, db)

    return {"ok": True}


@router.post("/webhook/stress")
async def webhook_stress(request: Request, db: Session = Depends(get_db)):
    """Receive stress detail push from Garmin Health API."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    items = body.get("stressDetails", [])
    if not isinstance(items, list):
        items = []

    for item in items:
        user = _find_user_for_item(item, db)
        if not user:
            print(f"[Garmin webhook/stress] UNKNOWN userId={item.get('userId')} – žádný účastník nenalezen")
            continue
        print(f"[Garmin webhook/stress] Data pro {user.participant_code} ({item.get('calendarDate')})")
        _store_garmin_user_id(user, item, db)

        date_val = _parse_date(item.get("calendarDate", ""))
        if not date_val:
            continue

        updates = {
            "stress_avg":        item.get("averageStressLevel"),
            "body_battery_high": item.get("bodyBatteryHighestValue"),
            "body_battery_low":  item.get("bodyBatteryLowestValue"),
        }
        _upsert_garmin(user.id, date_val, updates, db)

    return {"ok": True}
