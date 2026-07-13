# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from datetime import datetime
from database import get_db, User, UserRole, StudyPhase
from auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    role: str
    full_name: str
    participant_code: str | None

class RegisterRequest(BaseModel):
    email: str
    full_name: str
    password: str
    role: str = "participant"

@router.post("/token", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nesprávný email nebo heslo",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token({"sub": str(user.id), "role": str(user.role)})
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        role=user.role,
        full_name=user.full_name,
        participant_code=user.participant_code,
    )


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

@router.post("/change-password")
def change_password(data: ChangePasswordRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Nesprávné současné heslo")
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="Heslo musí mít alespoň 6 znaků")
    current_user.hashed_password = hash_password(data.new_password)
    db.commit()
    return {"ok": True}

@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    from tzutil import now_prague
    study_day = None
    if current_user.study_start_date and current_user.phase not in (None, 'preparation'):
        delta = (now_prague().date() - current_user.study_start_date.date()).days + 1
        study_day = max(1, min(delta, 21))
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role,
        "participant_code": current_user.participant_code,
        "phase": current_user.phase,
        "profession": current_user.profession,
        "consent_signed": current_user.consent_signed,
        "study_start_date": current_user.study_start_date.date().isoformat() if current_user.study_start_date else None,
        "study_day": study_day,
    }
