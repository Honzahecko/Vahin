# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import pandas as pd, json, io
from database import get_db, User, UserRole, QuestionnaireResponse, GarminData, NightShift
from auth import require_researcher

router = APIRouter(prefix="/api/export", tags=["export"])

@router.get("/questionnaires.xlsx", dependencies=[Depends(require_researcher)])
def export_questionnaires(db: Session = Depends(get_db)):
    rows = db.query(QuestionnaireResponse, User)\
             .join(User, User.id == QuestionnaireResponse.user_id)\
             .order_by(QuestionnaireResponse.filled_at).all()
    records = []
    for r, u in rows:
        answers = json.loads(r.answers)
        base = {
            "participant_code": u.participant_code,
            "full_name": u.full_name,
            "group": u.group,
            "phase": r.phase,
            "q_type": r.q_type,
            "filled_at": r.filled_at.isoformat() if r.filled_at else None,
            "shift_id": r.shift_id,
        }
        base.update(answers)
        records.append(base)
    df = pd.DataFrame(records)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Dotazniky")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=VAHIN_dotazniky.xlsx"},
    )

@router.get("/garmin.xlsx", dependencies=[Depends(require_researcher)])
def export_garmin(db: Session = Depends(get_db)):
    rows = db.query(GarminData, User)\
             .join(User, User.id == GarminData.user_id)\
             .order_by(GarminData.date).all()
    records = [{
        "participant_code": u.participant_code,
        "full_name": u.full_name,
        "group": u.group,
        "date": r.date.strftime("%Y-%m-%d") if r.date else None,
        "hrv_rmssd": r.hrv_rmssd,
        "hrv_weekly_avg": r.hrv_weekly_avg,
        "sleep_score": r.sleep_score,
        "sleep_hours": r.sleep_hours,
        "deep_sleep_min": r.deep_sleep_min,
        "rem_sleep_min": r.rem_sleep_min,
        "stress_avg": r.stress_avg,
        "steps": r.steps,
        "resting_hr": r.resting_hr,
        "max_hr": getattr(r, "max_hr", None),
        "spo2_avg": getattr(r, "spo2_avg", None),
        "respiration_avg": getattr(r, "respiration_avg", None),
        "body_battery_low": r.body_battery_low,
        "body_battery_high": getattr(r, "body_battery_high", None),
        "active_minutes": getattr(r, "active_minutes", None),
        "calories_active": getattr(r, "calories_active", None),
        "source": r.source,
    } for r, u in rows]
    df = pd.DataFrame(records)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Garmin")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=VAHIN_garmin.xlsx"},
    )

@router.get("/shifts.xlsx", dependencies=[Depends(require_researcher)])
def export_shifts(db: Session = Depends(get_db)):
    rows = db.query(NightShift, User)\
             .join(User, User.id == NightShift.user_id)\
             .order_by(NightShift.shift_date).all()
    records = [{
        "participant_code": u.participant_code,
        "full_name": u.full_name,
        "group": u.group,
        "shift_date": s.shift_date.strftime("%Y-%m-%d") if s.shift_date else None,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "end_time": s.end_time.isoformat() if s.end_time else None,
        "nurosym_used": s.nurosym_used,
        "nurosym_minutes": s.nurosym_minutes,
        "phase": s.phase,
        "notes": s.notes,
    } for s, u in rows]
    df = pd.DataFrame(records)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Smeny")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=VAHIN_smeny.xlsx"},
    )
