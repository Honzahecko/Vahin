# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from database import get_db, User, NightShift, StudyPhase
from auth import get_current_user, require_researcher
from tzutil import utc_iso

router = APIRouter(prefix="/api/shifts", tags=["shifts"])

class ShiftCreate(BaseModel):
    shift_date: datetime
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    nurosym_used: bool = False
    nurosym_minutes: Optional[int] = None
    notes: Optional[str] = None

def shift_to_dict(s: NightShift) -> dict:
    return {
        "id": s.id,
        "user_id": s.user_id,
        "shift_date": s.shift_date.isoformat() if s.shift_date else None,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "end_time": s.end_time.isoformat() if s.end_time else None,
        "nurosym_used": s.nurosym_used,
        "nurosym_minutes": s.nurosym_minutes,
        "phase": s.phase,
        "notes": s.notes,
        "created_at": utc_iso(s.created_at),
    }

@router.get("/my")
def my_shifts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    shifts = db.query(NightShift).filter(NightShift.user_id == current_user.id)\
               .order_by(NightShift.shift_date.desc()).limit(30).all()
    return [shift_to_dict(s) for s in shifts]

@router.post("/")
def create_shift(data: ShiftCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    shift = NightShift(
        user_id=current_user.id,
        shift_date=data.shift_date,
        start_time=data.start_time,
        end_time=data.end_time,
        nurosym_used=data.nurosym_used,
        nurosym_minutes=data.nurosym_minutes,
        phase=current_user.phase,
        notes=data.notes,
    )
    db.add(shift)
    if not current_user.study_start_date:
        current_user.study_start_date = data.shift_date
    db.commit()
    db.refresh(shift)
    return shift_to_dict(shift)

@router.get("/all", dependencies=[Depends(require_researcher)])
def all_shifts(db: Session = Depends(get_db)):
    shifts = db.query(NightShift).order_by(NightShift.shift_date.desc()).limit(200).all()
    return [shift_to_dict(s) for s in shifts]

@router.get("/user/{user_id}", dependencies=[Depends(require_researcher)])
def user_shifts(user_id: int, db: Session = Depends(get_db)):
    shifts = db.query(NightShift).filter(NightShift.user_id == user_id)\
               .order_by(NightShift.shift_date.desc()).all()
    return [shift_to_dict(s) for s in shifts]
