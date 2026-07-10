# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from database import get_db, User, UserRole, StudyGroup, StudyPhase, ProfessionType
from auth import hash_password, get_current_user, require_researcher

router = APIRouter(prefix="/api/participants", tags=["participants"])

class ParticipantCreate(BaseModel):
    email: str
    full_name: str
    password: str
    profession: Optional[str] = None
    group: Optional[str] = None
    notes: Optional[str] = None

class ParticipantUpdate(BaseModel):
    phase: Optional[str] = None
    group: Optional[str] = None
    profession: Optional[str] = None
    is_active: Optional[bool] = None
    consent_signed: Optional[bool] = None
    study_start_date: Optional[str] = None  # ISO date string "YYYY-MM-DD"
    notes: Optional[str] = None

def participant_to_dict(u: User) -> dict:
    # Vypočítej doporučenou fázi dle study_start_date
    recommended_phase = None
    study_day = None
    if u.study_start_date:
        delta = (datetime.utcnow() - u.study_start_date).days + 1
        study_day = max(1, delta)
        if study_day <= 7:
            recommended_phase = "phase1"
        elif study_day <= 14:
            recommended_phase = "washout"
        elif study_day <= 21:
            recommended_phase = "phase2"
        else:
            recommended_phase = "completed"
    return {
        "id": u.id,
        "participant_code": u.participant_code,
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "profession": u.profession,
        "group": u.group,
        "phase": u.phase,
        "is_active": u.is_active,
        "consent_signed": u.consent_signed,
        "consent_date": u.consent_date.isoformat() if u.consent_date else None,
        "study_start_date": u.study_start_date.date().isoformat() if u.study_start_date else None,
        "study_day": study_day,
        "recommended_phase": recommended_phase,
        "notes": u.notes,
        "garmin_user_id": getattr(u, "garmin_user_id", None),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }

def next_participant_code(db: Session) -> str:
    import re
    codes = [u.participant_code for u in db.query(User).filter(
        User.role == UserRole.participant,
        User.participant_code.isnot(None)
    ).all()]
    nums = [int(m.group(1)) for c in codes if (m := re.search(r'VAHIN-(\d+)', c))]
    next_num = (max(nums) + 1) if nums else 1
    existing = set(codes)
    while f"VAHIN-{next_num:03d}" in existing:
        next_num += 1
    return f"VAHIN-{next_num:03d}"

@router.get("/", dependencies=[Depends(require_researcher)])
def list_participants(db: Session = Depends(get_db)):
    users = db.query(User).filter(User.role == UserRole.participant).order_by(User.id).all()
    return [participant_to_dict(u) for u in users]

@router.post("/", dependencies=[Depends(require_researcher)])
def create_participant(data: ParticipantCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Email jiz existuje")
    code = next_participant_code(db)
    user = User(
        email=data.email,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=UserRole.participant,
        participant_code=code,
        profession=data.profession,
        group=data.group,
        notes=data.notes,
        phase=StudyPhase.preparation,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return participant_to_dict(user)

@router.patch("/{participant_id}", dependencies=[Depends(require_researcher)])
def update_participant(participant_id: int, data: ParticipantUpdate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == participant_id).first()
    if not user:
        raise HTTPException(404, "Ucastnik nenalezen")
    if data.phase is not None:      user.phase = data.phase
    if data.group is not None:      user.group = data.group
    if data.profession is not None: user.profession = data.profession
    if data.is_active is not None:  user.is_active = data.is_active
    if data.notes is not None:      user.notes = data.notes
    if data.study_start_date is not None:
        from datetime import date
        user.study_start_date = datetime.fromisoformat(data.study_start_date)
    if data.consent_signed is True:
        user.consent_signed = True
        user.consent_date = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return participant_to_dict(user)

@router.get("/{participant_id}", dependencies=[Depends(require_researcher)])
def get_participant(participant_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == participant_id).first()
    if not user:
        raise HTTPException(404, "Ucastnik nenalezen")
    return participant_to_dict(user)

@router.delete("/{participant_id}")
def delete_participant(participant_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in (UserRole.admin, UserRole.researcher):
        raise HTTPException(403, "Pouze admin/výzkumník")
    user = db.query(User).filter(User.id == participant_id, User.role == UserRole.participant).first()
    if not user:
        raise HTTPException(404, "Účastník nenalezen")
    # cascade: smaž data účastníka
    from database import NightShift, QuestionnaireResponse, GarminData, AdminNote
    db.query(AdminNote).filter(AdminNote.user_id == participant_id).delete()
    db.query(GarminData).filter(GarminData.user_id == participant_id).delete()
    db.query(QuestionnaireResponse).filter(QuestionnaireResponse.user_id == participant_id).delete()
    db.query(NightShift).filter(NightShift.user_id == participant_id).delete()
    db.delete(user)
    db.commit()
    return {"ok": True, "deleted": participant_id}

@router.post("/researcher", dependencies=[Depends(require_researcher)])
def create_researcher(data: ParticipantCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Email jiz existuje")
    user = User(
        email=data.email,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=UserRole.researcher,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return participant_to_dict(user)
