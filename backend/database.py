# -*- coding: utf-8 -*-
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, Enum as SAEnum, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import enum, os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "vahin.db")
DATABASE_URL = f"sqlite:///{os.path.abspath(DB_PATH)}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── Enums ──────────────────────────────────────────────────────────────────
class UserRole(str, enum.Enum):
    participant = "participant"
    researcher  = "researcher"
    admin       = "admin"

class StudyGroup(str, enum.Enum):
    A = "A"   # fáze 1 = aktivní tVNS, fáze 2 = placebo
    B = "B"   # fáze 1 = placebo, fáze 2 = aktivní tVNS

class StudyPhase(str, enum.Enum):
    preparation  = "preparation"   # před randomizací (souhlas + MEQ)
    phase1       = "phase1"        # týden 1 dny 1–7 (aktivní/sham dle skupiny)
    washout      = "washout"       # týden 2 dny 8–14 (vymývací perioda)
    phase2       = "phase2"        # týden 3 dny 15–21 (opačná podmínka)
    completed    = "completed"     # dokončeno

class ProfessionType(str, enum.Enum):
    doctor = "doctor"
    nurse  = "nurse"

# ── Modely ─────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True, index=True)
    participant_code = Column(String, unique=True, nullable=True)  # VAHIN-001 ...
    email         = Column(String, unique=True, index=True, nullable=False)
    full_name     = Column(String, nullable=False)
    hashed_password = Column(String, nullable=False)
    role          = Column(SAEnum(UserRole), default=UserRole.participant)
    profession    = Column(SAEnum(ProfessionType), nullable=True)
    group         = Column(SAEnum(StudyGroup), nullable=True)
    phase         = Column(SAEnum(StudyPhase), default=StudyPhase.preparation)
    is_active      = Column(Boolean, default=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    consent_signed = Column(Boolean, default=False)
    consent_date   = Column(DateTime, nullable=True)
    study_start_date = Column(DateTime, nullable=True)  # den 1 studie — od první noční směny
    shift_schedule   = Column(String(21), nullable=True)  # 21 znaků N/D/V pro každý den studie
    notes          = Column(Text, nullable=True)
    garmin_access_token  = Column(String, nullable=True)
    garmin_token_secret  = Column(String, nullable=True)
    garmin_user_id       = Column(String, nullable=True)

    shifts       = relationship("NightShift",   back_populates="user")
    responses    = relationship("QuestionnaireResponse", back_populates="user")
    garmin_data  = relationship("GarminData",   back_populates="user")
    cortisol_logs = relationship("CortisolLog", back_populates="user")
    cog_tests    = relationship("CognitiveTest", back_populates="user")

class NightShift(Base):
    __tablename__ = "night_shifts"
    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    shift_date    = Column(DateTime, nullable=False)
    start_time    = Column(DateTime, nullable=True)
    end_time      = Column(DateTime, nullable=True)
    nurosym_used  = Column(Boolean, default=False)
    nurosym_minutes = Column(Integer, nullable=True)
    phase         = Column(SAEnum(StudyPhase), nullable=True)
    notes         = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    user          = relationship("User", back_populates="shifts")
    responses     = relationship("QuestionnaireResponse", back_populates="shift")

class QuestionnaireType(str, enum.Enum):
    pre_shift_fatigue  = "pre_shift_fatigue"   # před směnou
    post_shift_fatigue = "post_shift_fatigue"  # po směně
    sleep_quality      = "sleep_quality"       # PSQI / spánek
    weekly_wellbeing   = "weekly_wellbeing"    # týdenní pohoda
    adverse_event      = "adverse_event"       # nežádoucí příhoda

class QuestionnaireResponse(Base):
    __tablename__ = "questionnaire_responses"
    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    shift_id      = Column(Integer, ForeignKey("night_shifts.id"), nullable=True)
    q_type        = Column(String, nullable=False)
    answers       = Column(Text, nullable=False)   # JSON string
    phase         = Column(SAEnum(StudyPhase), nullable=True)
    filled_at     = Column(DateTime, default=datetime.utcnow)
    duration_seconds = Column(Integer, nullable=True)
    # Zpětné doplnění: ke kterému dni odpověď patří ('YYYY-MM-DD'), None = den vyplnění
    target_date   = Column(String, nullable=True)

    user          = relationship("User",        back_populates="responses")
    shift         = relationship("NightShift",  back_populates="responses")

class GarminDataSource(str, enum.Enum):
    manual_csv = "manual_csv"
    api        = "api"

class GarminData(Base):
    __tablename__ = "garmin_data"
    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False)
    date            = Column(DateTime, nullable=False)
    hrv_rmssd       = Column(Float,   nullable=True)   # ms
    hrv_weekly_avg  = Column(Float,   nullable=True)
    sleep_score     = Column(Integer, nullable=True)   # 0-100
    sleep_hours     = Column(Float,   nullable=True)
    deep_sleep_min  = Column(Integer, nullable=True)
    rem_sleep_min   = Column(Integer, nullable=True)
    stress_avg      = Column(Integer, nullable=True)   # 0-100
    steps           = Column(Integer, nullable=True)
    resting_hr      = Column(Integer, nullable=True)   # BPM
    max_hr          = Column(Integer, nullable=True)   # BPM
    body_battery_low  = Column(Integer, nullable=True)
    body_battery_high = Column(Integer, nullable=True)
    spo2_avg        = Column(Integer, nullable=True)   # % SpO2
    respiration_avg = Column(Float,   nullable=True)   # dechy/min
    active_minutes  = Column(Integer, nullable=True)   # moderate+vigorous
    calories_active = Column(Integer, nullable=True)
    source          = Column(SAEnum(GarminDataSource), default=GarminDataSource.manual_csv)
    uploaded_at     = Column(DateTime, default=datetime.utcnow)

    user            = relationship("User", back_populates="garmin_data")

class CortisolSampleType(str, enum.Enum):
    day1   = "day1"   # den 1 – začátek fáze 1
    day7   = "day7"   # den 7 – konec fáze 1
    day15  = "day15"  # den 15 – začátek fáze 2
    day21  = "day21"  # den 21 – konec fáze 2

class CortisolTimepoint(str, enum.Enum):
    t0   = "t0"    # ihned po probuzení
    t15  = "t15"   # +15 minut
    t30  = "t30"   # +30 minut

class CortisolLog(Base):
    __tablename__ = "cortisol_logs"
    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    sample_type   = Column(SAEnum(CortisolSampleType), nullable=False)   # den1/den7/den15/den21
    timepoint     = Column(SAEnum(CortisolTimepoint), nullable=False)    # t0/t15/t30
    sample_time   = Column(DateTime, nullable=False)                     # čas odběru
    phase         = Column(SAEnum(StudyPhase), nullable=True)
    notes         = Column(Text, nullable=True)
    value_nmol_l  = Column(Float, nullable=True)                         # laboratorní výsledek (nmol/l)
    created_at    = Column(DateTime, default=datetime.utcnow)

    user          = relationship("User", back_populates="cortisol_logs")

class CogTestType(str, enum.Enum):
    reaction_time = "reaction_time"
    attention     = "attention"
    nback         = "nback"

class CognitiveTest(Base):
    __tablename__ = "cognitive_tests"
    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    test_type     = Column(SAEnum(CogTestType), nullable=False)
    phase         = Column(SAEnum(StudyPhase), nullable=True)
    score         = Column(Float, nullable=True)
    result_json   = Column(Text, nullable=True)   # detailní výsledky jako JSON
    duration_ms   = Column(Integer, nullable=True)
    taken_at      = Column(DateTime, default=datetime.utcnow)

    user          = relationship("User", back_populates="cog_tests")

class AdminNoteType(str, enum.Enum):
    note      = "note"
    deviation = "deviation"
    ae        = "ae"
    decision  = "decision"
    reminder  = "reminder"

class AdminNote(Base):
    __tablename__ = "admin_notes"
    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)   # NULL = projektová poznámka
    author_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    note_type   = Column(SAEnum(AdminNoteType), default=AdminNoteType.note)
    text        = Column(Text, nullable=False)
    phase       = Column(SAEnum(StudyPhase), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    resolved    = Column(Boolean, default=False)

class NotifType(str, enum.Enum):
    pre_shift         = "pre_shift"
    stimulation_start = "stimulation_start"  # 30 min stimulace na začátku směny
    stimulation_p1    = "stimulation_p1"     # pauza 1 (5-10 min)
    stimulation_p2    = "stimulation_p2"     # pauza 2 (5-10 min)
    stimulation_p3    = "stimulation_p3"     # pauza 3 (5-10 min)
    stimulation_end   = "stimulation_end"    # 30 min stimulace na konci směny
    post_shift        = "post_shift"
    stimulation_volno = "stimulation_volno"  # 15 min udržovací (volno/denní)
    cortisol_am       = "cortisol_am"        # kortizol ráno (den 1,7,15,21)
    cortisol_pm       = "cortisol_pm"        # kortizol odpoledne
    cortisol_eve      = "cortisol_eve"       # kortizol večer
    shift_entry       = "shift_entry"       # denní připomínka zadat směnu/volno
    psd_morning       = "psd_morning"       # spánkový deník každé ráno po probuzení
    pvt_post          = "pvt_post"          # PVT reakční test po noční směně
    weekly            = "weekly"
    reminder          = "reminder"
    cortisol          = "cortisol"           # legacy / generic

class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    endpoint   = Column(Text, nullable=False, unique=True)
    p256dh     = Column(String(500), nullable=False)
    auth       = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class NotificationSchedule(Base):
    __tablename__ = "notification_schedules"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    notif_type = Column(SAEnum(NotifType), nullable=False)
    hour       = Column(Integer, nullable=False)
    minute     = Column(Integer, default=0)
    days_mask       = Column(Integer, default=127)        # bitmask 1=Po…64=Ne, 127=každý den (legacy)
    study_days_mask = Column(Integer, default=0)          # bitmask bit0=den1…bit20=den21 studie; 0=nepoužívat
    enabled    = Column(Boolean, default=True)
    custom_msg = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

def create_tables():
    Base.metadata.create_all(bind=engine)
    _migrate_garmin_columns()
    _migrate_push_tables()

def _migrate_garmin_columns():
    """Přidá nové sloupce do garmin_data pokud ještě neexistují (SQLite nepodporuje auto-migrate)."""
    new_cols = [
        ("max_hr",           "INTEGER"),
        ("body_battery_high","INTEGER"),
        ("spo2_avg",         "INTEGER"),
        ("respiration_avg",  "REAL"),
        ("active_minutes",   "INTEGER"),
        ("calories_active",  "INTEGER"),
    ]
    with engine.connect() as conn:
        existing = {row[1] for row in conn.execute(
            __import__('sqlalchemy').text("PRAGMA table_info(garmin_data)")
        )}
        for col, typ in new_cols:
            if col not in existing:
                conn.execute(__import__('sqlalchemy').text(
                    f"ALTER TABLE garmin_data ADD COLUMN {col} {typ}"
                ))
        conn.commit()

def _migrate_push_tables():
    """Vytvoří push tabulky pokud chybí (pro případ staré DB)."""
    pass  # create_all() to pokryje
