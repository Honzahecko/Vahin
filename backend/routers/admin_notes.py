# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db, AdminNote, User, AdminNoteType
from auth import get_current_user, require_researcher

router = APIRouter(prefix="/api/notes", tags=["admin_notes"])

class NoteCreate(BaseModel):
    text: str
    note_type: str = "note"
    user_id: Optional[int] = None   # participant (None = projektová)

class NoteUpdate(BaseModel):
    text: Optional[str] = None
    resolved: Optional[bool] = None

def note_to_dict(n: AdminNote, author: User = None) -> dict:
    return {
        "id": n.id,
        "user_id": n.user_id,
        "author_id": n.author_id,
        "author_name": author.full_name if author else "—",
        "note_type": n.note_type,
        "text": n.text,
        "phase": n.phase,
        "created_at": n.created_at.isoformat(),
        "resolved": n.resolved,
    }

@router.post("/", dependencies=[Depends(require_researcher)])
def create_note(data: NoteCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from database import StudyPhase
    note = AdminNote(
        user_id=data.user_id,
        author_id=current_user.id,
        note_type=data.note_type,
        text=data.text,
    )
    if data.user_id:
        participant = db.query(User).filter(User.id == data.user_id).first()
        if participant:
            note.phase = participant.phase
    db.add(note)
    db.commit()
    db.refresh(note)
    return note_to_dict(note, current_user)

@router.get("/participant/{user_id}", dependencies=[Depends(require_researcher)])
def participant_notes(user_id: int, db: Session = Depends(get_db)):
    notes = db.query(AdminNote).filter(AdminNote.user_id == user_id)\
              .order_by(AdminNote.created_at.desc()).all()
    authors = {u.id: u for u in db.query(User).all()}
    return [note_to_dict(n, authors.get(n.author_id)) for n in notes]

@router.get("/project", dependencies=[Depends(require_researcher)])
def project_notes(db: Session = Depends(get_db)):
    notes = db.query(AdminNote).filter(AdminNote.user_id == None)\
              .order_by(AdminNote.created_at.desc()).limit(100).all()
    authors = {u.id: u for u in db.query(User).all()}
    return [note_to_dict(n, authors.get(n.author_id)) for n in notes]

@router.get("/all", dependencies=[Depends(require_researcher)])
def all_notes(db: Session = Depends(get_db)):
    notes = db.query(AdminNote).order_by(AdminNote.created_at.desc()).limit(200).all()
    authors = {u.id: u for u in db.query(User).all()}
    return [note_to_dict(n, authors.get(n.author_id)) for n in notes]

@router.patch("/{note_id}", dependencies=[Depends(require_researcher)])
def update_note(note_id: int, data: NoteUpdate, db: Session = Depends(get_db)):
    n = db.query(AdminNote).filter(AdminNote.id == note_id).first()
    if not n:
        raise HTTPException(404, "Poznámka nenalezena")
    if data.text is not None:
        n.text = data.text
    if data.resolved is not None:
        n.resolved = data.resolved
    db.commit()
    db.refresh(n)
    return note_to_dict(n)

@router.delete("/{note_id}", dependencies=[Depends(require_researcher)])
def delete_note(note_id: int, db: Session = Depends(get_db)):
    n = db.query(AdminNote).filter(AdminNote.id == note_id).first()
    if not n:
        raise HTTPException(404, "Poznámka nenalezena")
    db.delete(n)
    db.commit()
    return {"ok": True}
