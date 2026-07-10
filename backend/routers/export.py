# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import pandas as pd, json, io
from database import (get_db, User, UserRole, QuestionnaireResponse, GarminData,
                      NightShift, CortisolLog, CognitiveTest)
from auth import require_researcher

router = APIRouter(prefix="/api/export", tags=["export"])

def _xlsx_response(sheets: dict, filename: str) -> StreamingResponse:
    """sheets = {nazev_listu: seznam_dictu}"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, records in sheets.items():
            df = pd.DataFrame(records) if records else pd.DataFrame([{"info": "žádná data"}])
            df.to_excel(writer, index=False, sheet_name=name[:31])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

# ── Sběr dat ─────────────────────────────────────────────────────────────────

def _participant_records(db: Session) -> list:
    users = db.query(User).filter(User.role == UserRole.participant).order_by(User.id).all()
    return [{
        "participant_code": u.participant_code,
        "full_name": u.full_name,
        "email": u.email,
        "profession": u.profession,
        "group": u.group,
        "phase": u.phase,
        "is_active": u.is_active,
        "consent_signed": u.consent_signed,
        "consent_date": u.consent_date.isoformat() if u.consent_date else None,
        "study_start_date": u.study_start_date.date().isoformat() if u.study_start_date else None,
        "shift_schedule": u.shift_schedule,
        "garmin_connected": bool(getattr(u, "garmin_access_token", None)),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    } for u in users]

def _questionnaire_records(db: Session) -> list:
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
    return records

def _shift_records(db: Session) -> list:
    rows = db.query(NightShift, User)\
             .join(User, User.id == NightShift.user_id)\
             .order_by(NightShift.shift_date).all()
    return [{
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

def _garmin_records(db: Session) -> list:
    rows = db.query(GarminData, User)\
             .join(User, User.id == GarminData.user_id)\
             .order_by(GarminData.date).all()
    return [{
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

def _cortisol_records(db: Session) -> list:
    rows = db.query(CortisolLog, User)\
             .join(User, User.id == CortisolLog.user_id)\
             .order_by(CortisolLog.sample_time).all()
    return [{
        "participant_code": u.participant_code,
        "full_name": u.full_name,
        "group": u.group,
        "sample_type": c.sample_type,      # day1/day7/day15/day21
        "timepoint": c.timepoint,          # t0/t15/t30
        "sample_time": c.sample_time.isoformat() if c.sample_time else None,
        "phase": c.phase,
        "notes": c.notes,
    } for c, u in rows]

def _pvt_records(db: Session) -> list:
    rows = db.query(CognitiveTest, User)\
             .join(User, User.id == CognitiveTest.user_id)\
             .order_by(CognitiveTest.taken_at).all()
    records = []
    for t, u in rows:
        base = {
            "participant_code": u.participant_code,
            "full_name": u.full_name,
            "group": u.group,
            "test_type": t.test_type,
            "phase": t.phase,
            "score": t.score,
            "duration_ms": t.duration_ms,
            "taken_at": t.taken_at.isoformat() if t.taken_at else None,
        }
        # Rozbal detailní výsledky (medián RT, lapses…) do sloupců
        if t.result_json:
            try:
                detail = json.loads(t.result_json)
                if isinstance(detail, dict):
                    for k, v in detail.items():
                        if not isinstance(v, (list, dict)):
                            base[k] = v
            except Exception:
                pass
        records.append(base)
    return records

# ── Endpointy ────────────────────────────────────────────────────────────────

@router.get("/all.xlsx", dependencies=[Depends(require_researcher)])
def export_all(db: Session = Depends(get_db)):
    """Kompletní export studie – všechna data všech účastníků v jednom souboru."""
    return _xlsx_response({
        "Ucastnici": _participant_records(db),
        "Dotazniky": _questionnaire_records(db),
        "Smeny":     _shift_records(db),
        "Garmin":    _garmin_records(db),
        "Kortizol":  _cortisol_records(db),
        "PVT_testy": _pvt_records(db),
    }, "VAHIN_kompletni_export.xlsx")

@router.get("/questionnaires.xlsx", dependencies=[Depends(require_researcher)])
def export_questionnaires(db: Session = Depends(get_db)):
    return _xlsx_response({"Dotazniky": _questionnaire_records(db)}, "VAHIN_dotazniky.xlsx")

@router.get("/garmin.xlsx", dependencies=[Depends(require_researcher)])
def export_garmin(db: Session = Depends(get_db)):
    return _xlsx_response({"Garmin": _garmin_records(db)}, "VAHIN_garmin.xlsx")

@router.get("/shifts.xlsx", dependencies=[Depends(require_researcher)])
def export_shifts(db: Session = Depends(get_db)):
    return _xlsx_response({"Smeny": _shift_records(db)}, "VAHIN_smeny.xlsx")

@router.get("/cortisol.xlsx", dependencies=[Depends(require_researcher)])
def export_cortisol(db: Session = Depends(get_db)):
    return _xlsx_response({"Kortizol": _cortisol_records(db)}, "VAHIN_kortizol.xlsx")

@router.get("/pvt.xlsx", dependencies=[Depends(require_researcher)])
def export_pvt(db: Session = Depends(get_db)):
    return _xlsx_response({"PVT_testy": _pvt_records(db)}, "VAHIN_pvt.xlsx")
