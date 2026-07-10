# -*- coding: utf-8 -*-
"""
Web Push – správa subscriptions a notification schedules.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _PRAGUE = ZoneInfo("Europe/Prague")
except Exception:
    _PRAGUE = None
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
    study_days_mask: int = 0
    enabled: bool = True
    custom_msg: Optional[str] = None

class ShiftScheduleIn(BaseModel):
    schedule: str  # 21 znaků N/D/V

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
            study_days_mask=s.study_days_mask,
            enabled=s.enabled,
            custom_msg=s.custom_msg,
        ))
    db.commit()
    return {"ok": True, "count": len(schedules)}

@router.get("/shift-schedule/me")
def get_my_shift_schedule(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return current user's 21-day schedule and today's computed shift type."""
    schedule = current_user.shift_schedule or 'V' * 21
    schedule_set = any(c != 'V' for c in schedule)
    today_type = None
    if current_user.study_start_date:
        study_day = (datetime.utcnow().date() - current_user.study_start_date.date()).days + 1
        if 1 <= study_day <= 21:
            char = schedule[study_day - 1] if (study_day - 1) < len(schedule) else 'V'
            today_type = {'N': 'nocni', 'D': 'denni', 'V': 'volno'}.get(char, 'volno')
    return {"schedule": schedule, "today_type": today_type, "schedule_set": schedule_set}

@router.get("/shift-schedule/{user_id}", dependencies=[Depends(require_researcher)])
def get_shift_schedule(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Účastník nenalezen")
    return {"schedule": user.shift_schedule or "V" * 21}

@router.put("/shift-schedule/{user_id}", dependencies=[Depends(require_researcher)])
def save_shift_schedule(user_id: int, data: ShiftScheduleIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Účastník nenalezen")
    raw = data.schedule[:21]
    schedule = ''.join(c if c in 'NDV' else 'V' for c in raw).ljust(21, 'V')
    user.shift_schedule = schedule
    db.commit()
    return {"ok": True, "schedule": schedule}

@router.post("/sync-phase", dependencies=[Depends(require_researcher)])
def trigger_phase_sync():
    """Ruční spuštění fázové synchronizace notifikací (normálně běží v 00:05)."""
    from database import SessionLocal
    sync_phase_notifications(SessionLocal)
    return {"ok": True, "msg": "Fázová synchronizace proběhla."}

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
        "study_days_mask": s.study_days_mask or 0,
        "enabled": s.enabled,
        "custom_msg": s.custom_msg,
    }

NOTIF_TEXTS = {
    "pre_shift":         ("VAHIN – Před směnou",         "Vyplňte KSS před začátkem směny.",                                 "/?q=kss_pre"),
    "stimulation_start": ("VAHIN – Zahajte stimulaci",   "Zahajte 15minutovou tAVNS stimulaci (začátek směny).",             "/?tab=home"),
    "stimulation_p1":    ("VAHIN – Pauza: stimulace",    "Zahajte 15minutovou tAVNS stimulaci v pauze.",                     "/?tab=home"),
    "stimulation_p2":    ("VAHIN – Pauza: stimulace",    "Zahajte 15minutovou tAVNS stimulaci v pauze.",                     "/?tab=home"),
    "stimulation_p3":    ("VAHIN – Pauza: stimulace",    "Zahajte 15minutovou tAVNS stimulaci v pauze.",                     "/?tab=home"),
    "stimulation_end":   ("VAHIN – Závěrečná stimulace", "Zahajte 15minutovou tAVNS stimulaci (konec směny).",               "/?tab=home"),
    "post_shift":        ("VAHIN – Po směně",             "Vyplňte KSS a spánkový deník po směně.",                           "/?q=kss_post"),
    "stimulation_volno": ("VAHIN – Denní stimulace",      "Nezapomeňte na 15minutovou udržovací tAVNS stimulaci.",             "/?tab=home"),
    "cortisol_am":       ("VAHIN – Kortizol ráno",        "Odběr slin – vzorek č.1 (ráno, 30 min po probuzení, nalačno).",    "/"),
    "cortisol_pm":       ("VAHIN – Kortizol odpoledne",   "Odběr slin – vzorek č.2 (odpoledne, cca 6 h po ranním vzorku).",   "/"),
    "cortisol_eve":      ("VAHIN – Kortizol večer",       "Odběr slin – vzorek č.3 (večer, před spánkem).",                   "/"),
    "weekly":            ("VAHIN – Týdenní dotazníky",    "Vyplňte týdenní dotazníky MFI-20 a PSQI.",                         "/?q=mfi"),
    "shift_entry":       ("VAHIN – Připomínka",             "Nezapomeňte zadat směnu nebo den volna do aplikace.",              "/?tab=home"),
    "psd_morning":       ("VAHIN – Spánkový deník",         "Vyplňte spánkový deník po probuzení.",                             "/?q=psd"),
    "pvt_post":          ("VAHIN – Reakční test PVT",       "Čas na PVT reakční test — po skončení noční směny (~1 minuta).", "/?q=pvt"),
    "reminder":          ("VAHIN – Připomínka",            "Připomínka od výzkumného týmu VAHIN.",                             "/"),
    "stimulation":       ("VAHIN – Čas na stimulaci",     "Zahajte tAVNS stimulaci.",                                         "/?tab=home"),
    "cortisol":          ("VAHIN – Kortizol",              "Čas na odběr kortizolu ze slin.",                                  "/"),
}

STIM_TYPES = {
    'stimulation_start', 'stimulation_p1', 'stimulation_p2', 'stimulation_p3',
    'stimulation_end', 'stimulation_volno', 'stimulation',
}

def _build_notif_schedules(user_id: int, shift_schedule: str) -> list:
    """Sestaví seznam NotificationSchedule záznamů z rozvrhu N/D/V."""
    sch = (shift_schedule or 'V' * 21).ljust(21, 'V')
    mask_n = mask_d = mask_v = 0
    for d in range(21):
        c = sch[d]
        if c == 'N':   mask_n |= (1 << d)
        elif c == 'D': mask_d |= (1 << d)
        else:          mask_v |= (1 << d)

    cortisol_mask = (1<<0)|(1<<6)|(1<<14)|(1<<20)
    weekly_mask   = (1<<0)|(1<<6)|(1<<14)|(1<<20)
    all_mask      = (1<<21) - 1
    weeks1and3    = ((1<<7)-1) | (((1<<7)-1) << 14)
    stim_nocni    = mask_n & weeks1and3
    stim_volno    = (mask_v | mask_d) & weeks1and3

    # (notif_type, study_days_mask, hour, minute)
    entries = [
        ('pre_shift',         mask_n,       18, 15),
        ('stimulation_start', stim_nocni,   18, 15),
        ('stimulation_p1',    stim_nocni,   21,  0),
        ('stimulation_p2',    stim_nocni,    0,  0),
        ('stimulation_p3',    stim_nocni,    3,  0),
        ('stimulation_end',   stim_nocni,    5, 30),
        ('post_shift',        mask_n,        5, 30),
        ('pvt_post',          mask_n,        5, 30),
        ('stimulation_volno', stim_volno,    8,  0),
        ('psd_morning',       all_mask,      8,  0),
        ('cortisol_am',       cortisol_mask, 7, 30),
        ('cortisol_pm',       cortisol_mask,14,  0),
        ('cortisol_eve',      cortisol_mask,21,  0),
        ('weekly',            weekly_mask,  10,  0),
        ('reminder',          0,             9,  0),
    ]
    return [
        NotificationSchedule(
            user_id=user_id,
            notif_type=t,
            hour=h,
            minute=m,
            days_mask=127,
            study_days_mask=mask,
            enabled=mask > 0,
        )
        for t, mask, h, m in entries
    ]

def sync_phase_notifications(db_session_factory):
    """Každý den v 00:05: přepíná fáze a aktualizuje notifikace podle aktuálního study_day."""
    from sqlalchemy.orm import Session as DBSession
    now = datetime.now(_PRAGUE) if _PRAGUE else datetime.now()
    db: DBSession = db_session_factory()
    try:
        users = db.query(User).filter(User.study_start_date != None).all()
        for user in users:
            study_day = (now.date() - user.study_start_date.date()).days + 1

            # Den 1: přepni fázi a nastavení notifikací z rozvrhu
            if study_day == 1 and user.phase == 'prerandomizace':
                user.phase = 'phase1'
                db.query(NotificationSchedule).filter(NotificationSchedule.user_id == user.id).delete()
                for s in _build_notif_schedules(user.id, user.shift_schedule):
                    db.add(s)

            # Den 8: washout – vypni stimulace, přepni fázi
            elif study_day == 8 and user.phase == 'phase1':
                user.phase = 'washout'
                scheds = db.query(NotificationSchedule).filter(
                    NotificationSchedule.user_id == user.id,
                    NotificationSchedule.notif_type.in_(STIM_TYPES),
                ).all()
                for s in scheds:
                    s.enabled = False

            # Den 15: fáze 3 – zapni stimulace zpět
            elif study_day == 15 and user.phase == 'washout':
                user.phase = 'phase2'
                scheds = db.query(NotificationSchedule).filter(
                    NotificationSchedule.user_id == user.id,
                    NotificationSchedule.notif_type.in_(STIM_TYPES),
                ).all()
                for s in scheds:
                    s.enabled = True

        db.commit()
    finally:
        db.close()

def check_and_send(db_session_factory):
    """Spouštěno každou minutu APSchedulerem."""
    from sqlalchemy.orm import Session as DBSession
    now = datetime.now(_PRAGUE) if _PRAGUE else datetime.now()
    weekday_bit = 1 << now.weekday()   # Po=1, Út=2 … Ne=64

    db: DBSession = db_session_factory()
    try:
        schedules = db.query(NotificationSchedule).filter(
            NotificationSchedule.enabled == True,
            NotificationSchedule.hour   == now.hour,
            NotificationSchedule.minute == now.minute,
        ).all()

        for sched in schedules:
            sdm = sched.study_days_mask or 0
            if sdm:
                user = db.query(User).filter(User.id == sched.user_id).first()
                if not user or not user.study_start_date:
                    continue
                study_day = (now.date() - user.study_start_date.date()).days + 1
                if study_day < 1 or study_day > 21:
                    continue
                if not (sdm & (1 << (study_day - 1))):
                    continue
            else:
                if not (sched.days_mask & weekday_bit):
                    continue
            subs = db.query(PushSubscription).filter(
                PushSubscription.user_id == sched.user_id).all()
            title, body, url = NOTIF_TEXTS.get(sched.notif_type, ("VAHIN", "Připomínka", "/"))
            if sched.custom_msg:
                body = sched.custom_msg
            for sub in subs:
                push_manager.send_push(sub.endpoint, sub.p256dh, sub.auth, title, body, url)
    finally:
        db.close()
