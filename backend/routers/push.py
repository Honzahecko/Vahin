# -*- coding: utf-8 -*-
"""
Web Push – správa subscriptions a notification schedules.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from database import get_db, User, PushSubscription, NotificationSchedule, NotifType
from auth import get_current_user, require_researcher
import push_manager

router = APIRouter(prefix="/api/push", tags=["push"])

# ─── Schémata ───────────────────────────────────────────────────────────────

class SubscribeIn(BaseModel):
    endpoint: str
    p256dh: str
    auth: str

class ScheduleIn(BaseModel):
    notif_type: str
    hour: int
    minute: int = 0
    days_mask: int = 127
    enabled: bool = True
    custom_msg: Optional[str] = None

class TestPushIn(BaseModel):
    user_id: int
    title: str = "VAHIN Připomínka"
    body: str = "Nezapomeňte vyplnit dotazník."

# ─── Endpointy ──────────────────────────────────────────────────────────────

@router.get("/vapid-public-key")
def vapid_public_key():
    return {"publicKey": push_manager.get_public_key()}

@router.post("/subscribe")
def subscribe(data: SubscribeIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    existing = db.query(PushSubscription).filter(PushSubscription.endpoint == data.endpoint).first()
    if existing:
        existing.user_id = current_user.id
        existing.p256dh  = data.p256dh
        existing.auth    = data.auth
    else:
        db.add(PushSubscription(
            user_id=current_user.id,
            endpoint=data.endpoint,
            p256dh=data.p256dh,
            auth=data.auth,
        ))
    db.commit()
    return {"ok": True}

@router.delete("/subscribe")
def unsubscribe(data: SubscribeIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(PushSubscription)\
      .filter(PushSubscription.user_id == current_user.id,
              PushSubscription.endpoint == data.endpoint).delete()
    db.commit()
    return {"ok": True}

@router.get("/subscriptions/me")
def my_subscriptions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    subs = db.query(PushSubscription).filter(PushSubscription.user_id == current_user.id).all()
    return [{"id": s.id, "endpoint_short": s.endpoint[-30:], "created_at": s.created_at.isoformat()} for s in subs]

# ── Admin: schedules ────────────────────────────────────────────────────────

@router.get("/schedules/{user_id}", dependencies=[Depends(require_researcher)])
def get_schedules(user_id: int, db: Session = Depends(get_db)):
    scheds = db.query(NotificationSchedule).filter(NotificationSchedule.user_id == user_id).all()
    return [sched_to_dict(s) for s in scheds]

@router.put("/schedules/{user_id}", dependencies=[Depends(require_researcher)])
def set_schedules(user_id: int, schedules: List[ScheduleIn], db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Účastník nenalezen")
    db.query(NotificationSchedule).filter(NotificationSchedule.user_id == user_id).delete()
    for s in schedules:
        db.add(NotificationSchedule(
            user_id=user_id,
            notif_type=s.notif_type,
            hour=s.hour,
            minute=s.minute,
            days_mask=s.days_mask,
            enabled=s.enabled,
            custom_msg=s.custom_msg,
        ))
    db.commit()
    return {"ok": True, "count": len(schedules)}

@router.post("/test", dependencies=[Depends(require_researcher)])
def send_test_push(data: TestPushIn, db: Session = Depends(get_db)):
    subs = db.query(PushSubscription).filter(PushSubscription.user_id == data.user_id).all()
    if not subs:
        raise HTTPException(404, "Účastník nemá žádné push subscription (musí se nejdříve přihlásit na svém mobilu a zapnout notifikace).")
    sent = 0
    for s in subs:
        if push_manager.send_push(s.endpoint, s.p256dh, s.auth, data.title, data.body):
            sent += 1
    return {"ok": True, "sent": sent, "total": len(subs)}

# ─── Scheduler funkce (voláno z main.py) ───────────────────────────────────

def sched_to_dict(s: NotificationSchedule) -> dict:
    return {
        "id": s.id,
        "user_id": s.user_id,
        "notif_type": s.notif_type,
        "hour": s.hour,
        "minute": s.minute,
        "days_mask": s.days_mask,
        "enabled": s.enabled,
        "custom_msg": s.custom_msg,
    }

NOTIF_TEXTS = {
    "pre_shift":  ("VAHIN – Před směnou", "Vyplňte prosím dotazník únava před začátkem noční směny."),
    "post_shift": ("VAHIN – Po směně",    "Vyplňte prosím dotazník únava do 30 minut po skončení směny."),
    "weekly":     ("VAHIN – Týdenní pohoda", "Čas na týdenní dotazník – jak jste se měl/a tento týden?"),
    "reminder":   ("VAHIN – Připomínka",  "Připomínka od výzkumného týmu VAHIN."),
}

def check_and_send(db_session_factory):
    """Spouštěno každou minutu APSchedulerem."""
    from sqlalchemy.orm import Session as DBSession
    now = datetime.now()
    weekday_bit = 1 << now.weekday()   # Po=1, Út=2 … Ne=64

    db: DBSession = db_session_factory()
    try:
        schedules = db.query(NotificationSchedule).filter(
            NotificationSchedule.enabled == True,
            NotificationSchedule.hour   == now.hour,
            NotificationSchedule.minute == now.minute,
        ).all()

        for sched in schedules:
            if not (sched.days_mask & weekday_bit):
                continue
            subs = db.query(PushSubscription).filter(
                PushSubscription.user_id == sched.user_id).all()
            title, body = NOTIF_TEXTS.get(sched.notif_type, ("VAHIN", "Připomínka"))
            if sched.custom_msg:
                body = sched.custom_msg
            for sub in subs:
                push_manager.send_push(sub.endpoint, sub.p256dh, sub.auth, title, body)
    finally:
        db.close()
