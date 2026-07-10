# -*- coding: utf-8 -*-
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
import os, sys, shutil

sys.path.insert(0, os.path.dirname(__file__))

from database import create_tables, SessionLocal, User, UserRole, StudyPhase
from auth import hash_password
from routers import auth_router, participants, shifts, questionnaires, garmin, export, admin_notes, push
from routers import cortisol as cortisol_router  # noqa: F401 – router needed for its API endpoints
from routers import garmin_connect as garmin_connect_router

app = FastAPI(
    title="VAHIN Pilot Study",
    description="Platforma pro studii VAHIN – Pilot | INT2025002 | Nemocnice AGEL Trinec-Podles",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(participants.router)
app.include_router(shifts.router)
app.include_router(questionnaires.router)
app.include_router(garmin.router)
app.include_router(export.router)
app.include_router(admin_notes.router)
app.include_router(push.router)
app.include_router(cortisol_router.router)
app.include_router(garmin_connect_router.router)

# ── Statické soubory (frontend) ─────────────────────────────────────────────
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

@app.get("/")
def root():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "VAHIN API running", "docs": "/docs"}

@app.get("/sw.js")
def service_worker():
    sw_path = os.path.join(FRONTEND_DIR, "sw.js")
    from fastapi.responses import FileResponse as FR
    return FR(sw_path, media_type="application/javascript",
              headers={"Service-Worker-Allowed": "/"})

# ── Startup ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup():
    _migrate_db()   # ALTER TABLE musí být PŘED create_tables i _seed_admin
    create_tables()
    _seed_admin()
    import push_manager
    push_manager.init_vapid()
    from apscheduler.schedulers.background import BackgroundScheduler
    from routers.push import check_and_send, sync_phase_notifications
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_send, 'interval', minutes=1, args=[SessionLocal])
    scheduler.add_job(sync_phase_notifications, 'cron', hour=0, minute=5, args=[SessionLocal])
    scheduler.start()

def _migrate_db():
    """Přidá chybějící sloupce do existující DB. Volat před create_tables()."""
    import sqlite3, os
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "vahin.db")
    if not os.path.exists(db_path):
        return
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    def add_col(table, col, typedef):
        existing = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            print(f"[VAHIN migrate] Přidán sloupec {table}.{col}")

    add_col("users",                   "study_start_date",    "DATETIME")
    add_col("users",                   "shift_schedule",      "TEXT")
    add_col("users",                   "garmin_access_token", "TEXT")
    add_col("users",                   "garmin_token_secret", "TEXT")
    add_col("users",                   "garmin_user_id",      "TEXT")
    add_col("cortisol_logs",           "timepoint",           "TEXT NOT NULL DEFAULT 't0'")
    add_col("notification_schedules",  "study_days_mask",     "INTEGER DEFAULT 0")

    con.commit()
    con.close()

def _seed_admin():
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.email == "admin@vahin.cz").first():
            admin = User(
                email="admin@vahin.cz",
                full_name="Ing. Jan Hecko Ph.D. MBA LLM",
                hashed_password=hash_password("VAHIN2026!"),
                role=UserRole.admin,
                phase=StudyPhase.preparation,
            )
            db.add(admin)
            db.commit()
            print("[VAHIN] Admin ucet vytvoren: admin@vahin.cz / VAHIN2026!")
        else:
            print("[VAHIN] Admin ucet jiz existuje")
    finally:
        db.close()

# ── Dočasný DB upload (odstranit po nahrání!) ────────────────────────────────
_UPLOAD_SECRET = os.environ.get("DB_UPLOAD_SECRET", "")

@app.get("/db-upload", response_class=HTMLResponse)
def db_upload_form(key: str = ""):
    if not _UPLOAD_SECRET or key != _UPLOAD_SECRET:
        raise HTTPException(403, "Přidejte ?key=VÁŠ_SECRET do URL")
    return HTMLResponse(f"""
    <html><body style="font-family:sans-serif;padding:2rem;max-width:500px">
    <h2>📦 Nahrát databázi VAHIN</h2>
    <p style="color:#666">Nahrajte soubor <b>vahin.db</b> — stávající databáze na serveru bude přepsána.</p>
    <form method="post" action="/db-upload?key={key}" enctype="multipart/form-data">
      <input type="file" name="file" accept=".db" required style="margin:1rem 0;display:block">
      <button type="submit" style="background:#0f2744;color:white;padding:.75rem 2rem;border:none;border-radius:8px;font-size:1rem;cursor:pointer">
        Nahrát a přepsat DB
      </button>
    </form>
    </body></html>
    """)

@app.post("/db-upload")
async def db_upload(key: str = "", file: UploadFile = File(...)):
    if not _UPLOAD_SECRET or key != _UPLOAD_SECRET:
        raise HTTPException(403, "Nesprávný klíč")
    if not file.filename.endswith(".db"):
        raise HTTPException(400, "Soubor musí být .db")
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "vahin.db")
    backup  = db_path + ".bak"
    if os.path.exists(db_path):
        shutil.copy2(db_path, backup)
    with open(db_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;padding:2rem">
    <h2>✅ Databáze nahrána!</h2>
    <p>Soubor vahin.db byl úspěšně nahrazen. Restartujte service v Railway pro načtení.</p>
    <p style="color:#666;font-size:.9rem">Záloha původní DB je uložena jako vahin.db.bak</p>
    </body></html>
    """)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
