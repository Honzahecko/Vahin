# -*- coding: utf-8 -*-
"""
Garmin data – import CSV nebo ZIP/JSON exportu z Garmin Connect.
CSV: Date, HRV RMSSD, Sleep Score, Sleep Hours, Deep Sleep, REM Sleep,
     Stress Average, Steps, Resting Heart Rate, Body Battery Low
ZIP/JSON: standardní export z Garmin Connect (DI_CONNECT/DI-Connect-Aggregator/UDSFile*.json)
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import csv, io, json, zipfile
from database import get_db, User, GarminData, GarminDataSource
from auth import get_current_user, require_researcher

router = APIRouter(prefix="/api/garmin", tags=["garmin"])

COLUMN_MAP = {
    "date":              "date",
    "hrv rmssd":         "hrv_rmssd",
    "rmssd":             "hrv_rmssd",
    "hrv weekly avg":    "hrv_weekly_avg",
    "sleep score":       "sleep_score",
    "sleep hours":       "sleep_hours",
    "deep sleep":        "deep_sleep_min",
    "rem sleep":         "rem_sleep_min",
    "stress average":    "stress_avg",
    "avg stress":        "stress_avg",
    "steps":             "steps",
    "resting heart rate":"resting_hr",
    "resting hr":        "resting_hr",
    "body battery low":  "body_battery_low",
}

def parse_float(val):
    try:
        return float(str(val).replace(",", ".").strip()) if val else None
    except:
        return None

def parse_int(val):
    f = parse_float(val)
    return int(f) if f is not None else None

def parse_date(val):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except:
            continue
    return None

@router.post("/upload-csv")
def upload_garmin_csv(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    content = file.file.read().decode("utf-8-sig", errors="replace")
    reader  = csv.DictReader(io.StringIO(content))
    inserted, skipped = 0, 0

    for row in reader:
        normalized = {k.lower().strip(): v for k, v in row.items()}
        date_val = parse_date(normalized.get("date", ""))
        if not date_val:
            skipped += 1
            continue

        existing = db.query(GarminData)\
                     .filter(GarminData.user_id == current_user.id, GarminData.date == date_val)\
                     .first()
        if existing:
            skipped += 1
            continue

        record = GarminData(user_id=current_user.id, date=date_val, source=GarminDataSource.manual_csv)
        for csv_col, field in COLUMN_MAP.items():
            if csv_col == "date" or csv_col not in normalized:
                continue
            val = normalized[csv_col]
            if field in ("hrv_rmssd", "hrv_weekly_avg", "sleep_hours"):
                setattr(record, field, parse_float(val))
            else:
                setattr(record, field, parse_int(val))
        db.add(record)
        inserted += 1

    db.commit()
    return {"inserted": inserted, "skipped": skipped, "total_rows": inserted + skipped}

def _parse_uds_records(records: list, user_id: int, db: Session):
    """Zpracuj seznam daily summary záznamů z Garmin UDSFile."""
    inserted = skipped = 0
    for r in records:
        date_str = r.get("calendarDate")
        if not date_str:
            skipped += 1; continue
        date_val = parse_date(date_str)
        if not date_val:
            skipped += 1; continue

        existing = db.query(GarminData).filter(
            GarminData.user_id == user_id, GarminData.date == date_val
        ).first()
        if existing:
            skipped += 1; continue

        record = GarminData(user_id=user_id, date=date_val, source=GarminDataSource.manual_csv)

        # Kroky, vzdálenost, kalorie
        record.steps = r.get("totalSteps")
        record.calories_active = int(r["activeKilocalories"]) if r.get("activeKilocalories") else None

        # Tepová frekvence
        record.resting_hr = r.get("minAvgHeartRate") or r.get("minHeartRate")
        record.max_hr     = r.get("maxHeartRate")

        # SpO2 (kyslík v krvi)
        record.spo2_avg = r.get("lowestSpo2Value")

        # Dechová frekvence
        resp = r.get("respiration")
        if isinstance(resp, dict):
            record.respiration_avg = resp.get("avgWakingRespirationValue")

        # Aktivní minuty (moderate + vigorous)
        mod = r.get("moderateIntensityMinutes") or 0
        vig = r.get("vigorousIntensityMinutes") or 0
        if mod or vig:
            record.active_minutes = mod + vig

        # Stres — z allDayStress.aggregatorList[type=TOTAL]
        stress = r.get("allDayStress")
        if isinstance(stress, dict):
            for agg in stress.get("aggregatorList", []):
                if agg.get("type") == "TOTAL":
                    record.stress_avg = agg.get("averageStressLevel")
                    break

        # Body battery LOW + HIGH
        bb = r.get("bodyBattery")
        if isinstance(bb, dict):
            for stat in bb.get("bodyBatteryStatList", []):
                t = stat.get("bodyBatteryStatType")
                if t == "LOWEST":
                    record.body_battery_low = stat.get("statsValue")
                elif t == "HIGHEST":
                    record.body_battery_high = stat.get("statsValue")

        db.add(record)
        inserted += 1

    db.commit()
    return inserted, skipped


def _parse_sleep_records(records: list, user_id: int, db: Session):
    """Doplň spánková data (sleepData.json) do existujících záznamů."""
    updated = 0
    for r in records:
        date_str = r.get("calendarDate") or r.get("sleepStartTimestampLocal", "")[:10]
        if not date_str:
            continue
        date_val = parse_date(date_str)
        if not date_val:
            continue

        existing = db.query(GarminData).filter(
            GarminData.user_id == user_id, GarminData.date == date_val
        ).first()
        if not existing:
            existing = GarminData(user_id=user_id, date=date_val, source=GarminDataSource.manual_csv)
            db.add(existing)

        if r.get("sleepScores"):
            scores = r["sleepScores"]
            existing.sleep_score = scores.get("overall", {}).get("value") or scores.get("overallScore")
        if r.get("sleepTimeSeconds"):
            existing.sleep_hours = round(r["sleepTimeSeconds"] / 3600, 2)
        if r.get("deepSleepSeconds"):
            existing.deep_sleep_min = r["deepSleepSeconds"] // 60
        if r.get("remSleepSeconds"):
            existing.rem_sleep_min = r["remSleepSeconds"] // 60
        updated += 1

    db.commit()
    return updated


@router.post("/upload-zip")
def upload_garmin_zip(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Nahrát celý Garmin Connect export (ZIP soubor)."""
    raw = file.file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Soubor není platný ZIP archiv")

    uds_records = []
    sleep_records = []
    found_files = []

    for name in zf.namelist():
        lower = name.lower()
        # UDSFile – daily summary
        if "di-connect-aggregator" in lower and "udsfile" in lower and name.endswith(".json"):
            data = json.loads(zf.read(name))
            if isinstance(data, list):
                uds_records.extend(data)
                found_files.append(name.split("/")[-1])
        # Sleep data
        elif "di-connect-wellness" in lower and "sleepdata" in lower and name.endswith(".json"):
            data = json.loads(zf.read(name))
            if isinstance(data, list):
                sleep_records.extend(r for r in data if isinstance(r, dict) and len(r) > 2)
                found_files.append(name.split("/")[-1])

    if not uds_records and not sleep_records:
        raise HTTPException(400, "ZIP neobsahuje žádná Garmin zdravotní data (UDSFile / sleepData)")

    ins, skp = _parse_uds_records(uds_records, current_user.id, db)
    upd = _parse_sleep_records(sleep_records, current_user.id, db) if sleep_records else 0

    return {
        "inserted": ins, "skipped": skp,
        "sleep_updated": upd,
        "files_found": found_files,
    }


@router.post("/upload-json")
def upload_garmin_json(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Nahrát přímo UDSFile*.json nebo sleepData*.json soubor."""
    content = file.file.read().decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(400, "Soubor není platný JSON")

    if not isinstance(data, list):
        raise HTTPException(400, "JSON musí být pole záznamů")

    fname = (file.filename or "").lower()
    if "sleep" in fname:
        valid = [r for r in data if isinstance(r, dict) and len(r) > 2]
        upd = _parse_sleep_records(valid, current_user.id, db)
        return {"sleep_updated": upd, "total": len(data)}
    else:
        ins, skp = _parse_uds_records(data, current_user.id, db)
        return {"inserted": ins, "skipped": skp, "total": len(data)}


@router.get("/my")
def my_garmin_data(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    records = db.query(GarminData).filter(GarminData.user_id == current_user.id)\
                .order_by(GarminData.date.desc()).limit(90).all()
    return [garmin_to_dict(r) for r in records]

@router.get("/all", dependencies=[Depends(require_researcher)])
def all_garmin_data(db: Session = Depends(get_db)):
    records = db.query(GarminData).order_by(GarminData.date.desc()).limit(500).all()
    result = []
    for r in records:
        d = garmin_to_dict(r)
        d["user_id"] = r.user_id
        result.append(d)
    return result

@router.get("/user/{user_id}", dependencies=[Depends(require_researcher)])
def user_garmin(user_id: int, db: Session = Depends(get_db)):
    records = db.query(GarminData).filter(GarminData.user_id == user_id)\
                .order_by(GarminData.date.desc()).limit(365).all()
    return [garmin_to_dict(r) for r in records]

def garmin_to_dict(r: GarminData) -> dict:
    return {
        "id": r.id,
        "date": r.date.strftime("%Y-%m-%d") if r.date else None,
        "hrv_rmssd":        r.hrv_rmssd,
        "hrv_weekly_avg":   r.hrv_weekly_avg,
        "sleep_score":      r.sleep_score,
        "sleep_hours":      r.sleep_hours,
        "deep_sleep_min":   r.deep_sleep_min,
        "rem_sleep_min":    r.rem_sleep_min,
        "stress_avg":       r.stress_avg,
        "steps":            r.steps,
        "resting_hr":       r.resting_hr,
        "max_hr":           getattr(r, "max_hr", None),
        "body_battery_low":  r.body_battery_low,
        "body_battery_high": getattr(r, "body_battery_high", None),
        "spo2_avg":         getattr(r, "spo2_avg", None),
        "respiration_avg":  getattr(r, "respiration_avg", None),
        "active_minutes":   getattr(r, "active_minutes", None),
        "calories_active":  getattr(r, "calories_active", None),
        "source":           r.source,
    }
