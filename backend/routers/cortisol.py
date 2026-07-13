# -*- coding: utf-8 -*-
"""
Kortizol – záznam odběrů slin (T0 / T+15 / T+30) ve dnech 1, 7, 15, 21 studie.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta

from database import get_db, User, CortisolLog, CortisolSampleType, CortisolTimepoint, StudyPhase
from auth import get_current_user, require_researcher
from tzutil import utc_iso, now_prague
import push_manager

router = APIRouter(prefix="/api/cortisol", tags=["cortisol"])

# ─── Schémata ────────────────────────────────────────────────────────────────

class CortisolLogIn(BaseModel):
    sample_type: str          # day1 / day7 / day15 / day21
    timepoint:   str          # t0 / t15 / t30
    sample_time: str          # ISO datetime – čas odběru
    notes: Optional[str] = None

class CortisolLogOut(BaseModel):
    id:          int
    sample_type: str
    timepoint:   str
    sample_time: str
    phase:       Optional[str]
    notes:       Optional[str]
    created_at:  str

# ─── Pomocné funkce ──────────────────────────────────────────────────────────

CORTISOL_DAYS = {1: "day1", 7: "day7", 15: "day15", 21: "day21"}

def current_study_day(user: User) -> Optional[int]:
    if not user.study_start_date:
        return None
    # Den studie počítej v českém čase (konzistentně s plánovačem notifikací),
    # jinak se den přepne až ve 2:00 ráno letního času.
    delta = (now_prague().date() - user.study_start_date.date()).days + 1
    return max(1, delta)

def is_cortisol_day(user: User) -> bool:
    day = current_study_day(user)
    return day in CORTISOL_DAYS

def sample_type_for_user(user: User) -> Optional[str]:
    day = current_study_day(user)
    return CORTISOL_DAYS.get(day)

def log_to_dict(log: CortisolLog) -> dict:
    return {
        "id":          log.id,
        "sample_type": log.sample_type,
        "timepoint":   log.timepoint,
        "sample_time": utc_iso(log.sample_time),
        "phase":       log.phase,
        "notes":       log.notes,
        "created_at":  utc_iso(log.created_at),
    }

# ─── Endpointy – účastník ────────────────────────────────────────────────────

@router.get("/my")
def get_my_cortisol(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Vrátí vlastní záznamy kortizolu + info o dnešním dni studie."""
    logs = db.query(CortisolLog).filter(
        CortisolLog.user_id == current_user.id
    ).order_by(CortisolLog.sample_time.desc()).all()

    day  = current_study_day(current_user)
    stype = sample_type_for_user(current_user)

    return {
        "study_day":    day,
        "is_cortisol_day": day in CORTISOL_DAYS if day else False,
        "sample_type_today": stype,
        "logs": [log_to_dict(l) for l in logs],
    }

@router.post("/")
def log_cortisol(
    data: CortisolLogIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Zaznamenat odběr vzorku kortizolu."""
    try:
        st = CortisolSampleType(data.sample_type)
        tp = CortisolTimepoint(data.timepoint)
    except ValueError:
        raise HTTPException(400, "Neplatný typ odběru nebo timepoint")

    log = CortisolLog(
        user_id     = current_user.id,
        sample_type = st,
        timepoint   = tp,
        sample_time = datetime.fromisoformat(data.sample_time),
        phase       = current_user.phase,
        notes       = data.notes,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log_to_dict(log)

@router.delete("/{log_id}")
def delete_cortisol_log(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    log = db.query(CortisolLog).filter(
        CortisolLog.id == log_id,
        CortisolLog.user_id == current_user.id
    ).first()
    if not log:
        raise HTTPException(404, "Záznam nenalezen")
    db.delete(log)
    db.commit()
    return {"ok": True}

# ─── Endpointy – výzkumník ───────────────────────────────────────────────────

@router.get("/participant/{user_id}", dependencies=[Depends(require_researcher)])
def get_participant_cortisol(user_id: int, db: Session = Depends(get_db)):
    logs = db.query(CortisolLog).filter(
        CortisolLog.user_id == user_id
    ).order_by(CortisolLog.sample_time).all()
    return [log_to_dict(l) for l in logs]

@router.get("/all", dependencies=[Depends(require_researcher)])
def get_all_cortisol(db: Session = Depends(get_db)):
    """Přehled všech odběrů pro export."""
    logs = db.query(CortisolLog).order_by(CortisolLog.sample_time).all()
    return [{"user_id": l.user_id, **log_to_dict(l)} for l in logs]

# ─── Push notifikace pro kortizolové dny ─────────────────────────────────────

def send_cortisol_push_all(SessionLocal, timepoint: str, title: str, body: str):
    """Voláno APSchedulerem – pošle push všem účastníkům s kortizolovým dnem."""
    from database import PushSubscription
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_active == True, User.study_start_date.isnot(None)).all()
        for u in users:
            if not is_cortisol_day(u):
                continue
            subs = db.query(PushSubscription).filter(PushSubscription.user_id == u.id).all()
            for sub in subs:
                push_manager.send_push(sub.endpoint, sub.p256dh, sub.auth,
                                       title=title, body=body, url="/app/")
    finally:
        db.close()

@router.post("/notify-cortisol-now", dependencies=[Depends(require_researcher)])
def notify_cortisol_now(db: Session = Depends(get_db)):
    """
    Admin spustí ručně, nebo scheduler volá každé ráno.
    Pošle push T0 všem účastníkům, kteří mají dnes kortizolový den.
    Systém pak posílá T+15 a T+30 přes APScheduler (viz main.py).
    """
    from database import PushSubscription
    users = db.query(User).filter(User.is_active == True, User.study_start_date.isnot(None)).all()
    sent = 0
    for u in users:
        if not is_cortisol_day(u):
            continue
        day = current_study_day(u)
        subs = db.query(PushSubscription).filter(PushSubscription.user_id == u.id).all()
        for sub in subs:
            ok = push_manager.send_push(
                sub.endpoint, sub.p256dh, sub.auth,
                title=f"🧪 Kortizol – den {day} studie",
                body="Čas na první odběr slin (T0). Odeberte ihned po probuzení.",
                url="/app/"
            )
            if ok:
                sent += 1
    return {"sent": sent}
