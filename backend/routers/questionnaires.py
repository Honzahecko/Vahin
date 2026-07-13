# -*- coding: utf-8 -*-
"""
Dotazníky: únava před/po směně (VAS), kvalita spánku (PSQI subset), týdenní pohoda.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import json
from database import get_db, User, QuestionnaireResponse, QuestionnaireType, StudyPhase
from auth import get_current_user, require_researcher
from tzutil import utc_iso

router = APIRouter(prefix="/api/questionnaires", tags=["questionnaires"])

# ── Definice otázek ──────────────────────────────────────────────────────────
QUESTIONNAIRE_DEFINITIONS = {
    "pre_shift_fatigue": {
        "title": "Hodnoceni unavy - pred smenu",
        "description": "Vyplnte prosim pred zahajenim nocni smeny.",
        "frequency": "per_shift_before",
        "icon": "sunrise",
        "color": "blue",
        "questions": [
            {"id": "vas_fatigue",    "type": "vas",    "label": "Jak unaveni se citite prave ted?", "min_label": "Vubec unaven", "max_label": "Maximalne unaven"},
            {"id": "vas_alertness",  "type": "vas",    "label": "Jak bdeli a soustredeni se citite?", "min_label": "Vubec bdely", "max_label": "Maximalne bdely"},
            {"id": "last_sleep_h",   "type": "number", "label": "Kolik hodin jste spal/a pred touto smenou?", "min": 0, "max": 24},
            {"id": "sleep_quality",  "type": "scale5", "label": "Jak hodnotite kvalitu tohoto spanku?", "options": ["Velmi spatny","Spatny","Prumerni","Dobry","Vyborny"]},
            {"id": "feel_ready",     "type": "bool",   "label": "Citite se pripraven/a na smenu?"},
        ]
    },
    "post_shift_fatigue": {
        "title": "Hodnoceni unavy - po smene",
        "description": "Vyplnte prosim po skonceni nocni smeny (do 30 minut).",
        "frequency": "per_shift_after",
        "icon": "sunset",
        "color": "orange",
        "questions": [
            {"id": "vas_fatigue",    "type": "vas",    "label": "Jak unaveni se citite po smene?", "min_label": "Vubec unaven", "max_label": "Maximalne unaven"},
            {"id": "vas_alertness",  "type": "vas",    "label": "Jak soustredeni jste byli behem smeny?", "min_label": "Vubec", "max_label": "Plne"},
            {"id": "errors_made",    "type": "bool",   "label": "Doslo behem smeny k nejake chybe nebo skoro-miss?"},
            {"id": "nurosym_effect", "type": "scale5", "label": "Pokud jste pouzili Nurosym - jak hodnotite jeho efekt?", "options": ["Zadny","Mirny","Stredni","Znatelny","Vyrazny"], "optional": True},
            {"id": "shift_difficulty","type": "scale5","label": "Jak narocna byla smena celkove?", "options": ["Velmi lehka","Lehka","Prumerna","Narocna","Velmi narocna"]},
            {"id": "notes",          "type": "text",   "label": "Poznamky (volitelne)", "optional": True},
        ]
    },
    "sleep_quality": {
        "title": "Kvalita spanku - tydenni hodnoceni",
        "description": "Vyplnte jednou tydne, nejlepe v nedeli rano.",
        "frequency": "weekly",
        "icon": "moon",
        "color": "indigo",
        "questions": [
            {"id": "bed_time",       "type": "time",   "label": "V kolik hodin obvykle chodite spat?"},
            {"id": "sleep_latency",  "type": "number", "label": "Za jak dlouho obvykle usnete (minuty)?", "min": 0, "max": 180},
            {"id": "wake_time",      "type": "time",   "label": "V kolik hodin obvykle vstanete?"},
            {"id": "actual_sleep_h", "type": "number", "label": "Skutecny pocet hodin spanku (bez probuzeni)?", "min": 0, "max": 14},
            {"id": "disturbances",   "type": "scale4", "label": "Jak casto jste se budil/a v noci?", "options": ["Nikdy","Jednou za tyden","2-3x tydne","Vice nez 3x"]},
            {"id": "sleep_meds",     "type": "bool",   "label": "Uzival/a jste leky na spani?"},
            {"id": "daytime_tired",  "type": "scale4", "label": "Jak casto jste byl/a behem dne unaven?", "options": ["Nikdy","Jednou za tyden","2-3x tydne","Kazdy den"]},
            {"id": "overall_sleep",  "type": "scale4", "label": "Jak hodnotite celkovou kvalitu spanku za tento tyden?", "options": ["Velmi spatna","Spatna","Dobra","Velmi dobra"]},
        ]
    },
    "weekly_wellbeing": {
        "title": "Tydeni pohoda",
        "description": "Kratke tydenni zhodnoceni celkoveho stavu.",
        "frequency": "weekly",
        "icon": "heart",
        "color": "green",
        "questions": [
            {"id": "vas_wellbeing",  "type": "vas",    "label": "Jak jste se celkove citil/a tento tyden?", "min_label": "Velmi spatne", "max_label": "Vyborne"},
            {"id": "stress_level",   "type": "scale5", "label": "Uroven stresu tento tyden?", "options": ["Zadny","Mirny","Stredni","Vysoky","Extremni"]},
            {"id": "work_performance","type": "scale5","label": "Jak hodnotite svuj pracovni vykon?", "options": ["Velmi spatny","Spatny","Prumerni","Dobry","Vyborny"]},
            {"id": "motivation",     "type": "scale5", "label": "Motivace ke studii?", "options": ["Zadna","Mala","Stredni","Vysoka","Maximalni"]},
            {"id": "side_effects",   "type": "bool",   "label": "Zaznamenal/a jste jakekoli nezadouci ucinky?"},
            {"id": "side_effects_desc","type":"text",  "label": "Popis nezadoucich ucinku (pokud ano)", "optional": True},
        ]
    },
    "kss": {
        "title": "KSS – Karolinska Sleepiness Scale",
        "description": "Vyplňte PŘED a PO každé směně.",
        "frequency": "per_shift",
        "icon": "moon",
        "color": "purple",
        "questions": [
            {"id": "sleepiness_level", "type": "choice", "label": "Jak ospalý/á se právě teď cítíte?",
             "options": [
                {"label": "1 – Velmi bdělý/á", "value": 1},
                {"label": "2 – Bdělý/á", "value": 2},
                {"label": "3 – Ani bdělý/á, ani ospalý/á", "value": 3},
                {"label": "4 – Lehce ospalý/á", "value": 4},
                {"label": "5 – Ospalý/á, ale bez potíží zůstat vzhůru", "value": 5},
                {"label": "6 – Známky ospalosti, občasné zívání, snížená pozornost", "value": 6},
                {"label": "7 – Ospalý/á, ale bez úsilí zůstávám vzhůru", "value": 7},
                {"label": "8 – Velmi ospalý/á, mám problém zůstat vzhůru", "value": 8},
                {"label": "9 – Velmi ospalý/á, téměř v mikrospánku, bojuji se zůstat vzhůru", "value": 9},
             ]},
        ]
    },
    "kss_pre": {
        "title": "KSS – Ospalost před směnou",
        "description": "Vyplňte těsně před nástupem na směnu.",
        "frequency": "per_shift_before",
        "icon": "moon",
        "color": "indigo",
        "questions": [
            {"id": "sleepiness_level", "type": "choice", "label": "Jak ospalý/á se právě teď cítíte (před směnou)?",
             "options": [
                {"label": "1 – Velmi bdělý/á", "value": 1},
                {"label": "2 – Bdělý/á", "value": 2},
                {"label": "3 – Ani bdělý/á, ani ospalý/á", "value": 3},
                {"label": "4 – Lehce ospalý/á", "value": 4},
                {"label": "5 – Ospalý/á, ale bez potíží zůstat vzhůru", "value": 5},
                {"label": "6 – Známky ospalosti, občasné zívání, snížená pozornost", "value": 6},
                {"label": "7 – Ospalý/á, ale bez úsilí zůstávám vzhůru", "value": 7},
                {"label": "8 – Velmi ospalý/á, mám problém zůstat vzhůru", "value": 8},
                {"label": "9 – Velmi ospalý/á, téměř v mikrospánku, bojuji se zůstat vzhůru", "value": 9},
             ]},
        ]
    },
    "kss_post": {
        "title": "KSS – Ospalost po směně",
        "description": "Vyplňte ihned po skončení směny.",
        "frequency": "per_shift_after",
        "icon": "moon",
        "color": "indigo",
        "questions": [
            {"id": "sleepiness_level", "type": "choice", "label": "Jak ospalý/á se právě teď cítíte (po směně)?",
             "options": [
                {"label": "1 – Velmi bdělý/á", "value": 1},
                {"label": "2 – Bdělý/á", "value": 2},
                {"label": "3 – Ani bdělý/á, ani ospalý/á", "value": 3},
                {"label": "4 – Lehce ospalý/á", "value": 4},
                {"label": "5 – Ospalý/á, ale bez potíží zůstat vzhůru", "value": 5},
                {"label": "6 – Známky ospalosti, občasné zívání, snížená pozornost", "value": 6},
                {"label": "7 – Ospalý/á, ale bez úsilí zůstávám vzhůru", "value": 7},
                {"label": "8 – Velmi ospalý/á, mám problém zůstat vzhůru", "value": 8},
                {"label": "9 – Velmi ospalý/á, téměř v mikrospánku, bojuji se zůstat vzhůru", "value": 9},
             ]},
        ]
    },
    "psd": {
        "title": "PSD – Spánkový deník",
        "description": "Vyplňte každé ráno po probuzení. Týká se spánku z minulé noci.",
        "frequency": "daily_morning",
        "icon": "bed",
        "color": "blue",
        "questions": [
            {"id": "sleep_latency", "type": "choice", "label": "Jak dlouho vám trvalo usnout?",
             "options": [{"label": "Méně než 15 minut", "value": "a"}, {"label": "15–30 minut", "value": "b"}, {"label": "30–45 minut", "value": "c"}, {"label": "Více než 45 minut", "value": "d"}]},
            {"id": "total_sleep_time", "type": "choice", "label": "Kolik hodin jste celkem spal/a?",
             "options": [{"label": "8 a více hodin", "value": "a"}, {"label": "6–8 hodin", "value": "b"}, {"label": "4–6 hodin", "value": "c"}, {"label": "Méně než 4 hodiny", "value": "d"}]},
            {"id": "night_awakenings", "type": "choice", "label": "Jak často jste se během noci probudil/a?",
             "options": [{"label": "Vůbec", "value": "a"}, {"label": "1–2×", "value": "b"}, {"label": "3–4×", "value": "c"}, {"label": "Více než 4×", "value": "d"}]},
            {"id": "sleep_quality", "type": "choice", "label": "Jak hodnotíte svůj spánek? (1=ideální, 4=špatný)",
             "options": [{"label": "Velmi dobrý", "value": "a"}, {"label": "Dobrý", "value": "b"}, {"label": "Špatný", "value": "c"}, {"label": "Velmi špatný", "value": "d"}]},
        ]
    },
    "tavns_diary": {
        "title": "Deník tAVNS stimulace",
        "description": "Vyplňte po KAŽDÉ stimulaci.",
        "frequency": "per_stimulation",
        "icon": "zap",
        "color": "yellow",
        "questions": [
            {"id": "stimulation_date", "type": "date", "label": "Datum stimulace"},
            {"id": "stimulation_timing", "type": "choice", "label": "Kdy proběhla stimulace?",
             "options": [
                {"label": "Na začátku směny (15–30 min)", "value": "shift_start"},
                {"label": "Krátká stimulace v pauze (5–10 min)", "value": "short_break"},
                {"label": "Krátká stimulace při pocitu únavy (5–10 min)", "value": "fatigue_short"},
                {"label": "Na konci směny (15–30 min)", "value": "shift_end"},
                {"label": "Udržovací stimulace ve volném dni (15 min)", "value": "maintenance_day_off"},
             ]},
            {"id": "stimulation_start_time", "type": "time", "label": "Čas zahájení stimulace"},
            {"id": "stimulation_duration_minutes", "type": "number", "label": "Délka stimulace (minuty)", "min": 0, "max": 120},
            {"id": "stimulation_perception", "type": "choice", "label": "Jak jste vnímal/a stimulaci?",
             "options": [
                {"label": "1 – Vůbec jsem ji nevnímal/a", "value": 1},
                {"label": "2 – Sotva znatelná", "value": 2},
                {"label": "3 – Mírně znatelná, příjemná", "value": 3},
                {"label": "4 – Zřetelně znatelná, příjemná", "value": 4},
                {"label": "5 – Silná, ale stále příjemná", "value": 5},
                {"label": "6 – Silná a nepříjemná", "value": 6},
             ]},
            {"id": "completed_as_planned", "type": "choice", "label": "Dokončil/a jste stimulaci dle plánu?",
             "options": [
                {"label": "Ano, plně dokončena", "value": "yes_full"},
                {"label": "Ano, ale zkrácena", "value": "yes_shortened"},
                {"label": "Ne, přerušena", "value": "no_interrupted"},
             ]},
            {"id": "interruption_reason", "type": "textarea", "label": "Pokud přerušena/zkrácena – důvod:", "optional": True},
            {"id": "stimulation_notes", "type": "textarea", "label": "Poznámky / subjektivní pocity:", "optional": True},
        ]
    },
    "psqi": {
        "title": "PSQI – Pittsburgh Sleep Quality Index",
        "description": "Vyplňte 1× týdně. Otázky se týkají vašich spánkových návyků ZA POSLEDNÍ TÝDEN.",
        "frequency": "weekly",
        "icon": "moon",
        "color": "indigo",
        "questions": [
            {"id": "study_week", "type": "number", "label": "Týden studie č.", "min": 1, "max": 52},
            {"id": "bed_time", "type": "time", "label": "1. V kolik hodin obvykle chodíte spát?"},
            {"id": "sleep_latency_category", "type": "choice", "label": "2. Kolik minut obvykle trvá, než usnete?",
             "options": [{"label": "Méně než 15 minut", "value": "a"}, {"label": "15–30 minut", "value": "b"}, {"label": "31–60 minut", "value": "c"}, {"label": "Více než 60 minut", "value": "d"}]},
            {"id": "wake_time", "type": "time", "label": "3. V kolik hodin obvykle vstáváte ráno?"},
            {"id": "actual_sleep_duration_category", "type": "choice", "label": "4. Kolik hodin skutečně spíte za noc?",
             "options": [{"label": "Více než 7 hodin", "value": "a"}, {"label": "6–7 hodin", "value": "b"}, {"label": "5–6 hodin", "value": "c"}, {"label": "Méně než 5 hodin", "value": "d"}]},
            {"id": "cannot_sleep_30_min", "type": "choice", "label": "5. Nemůžete usnout do 30 minut",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "wake_middle_or_early", "type": "choice", "label": "6. Budíte se uprostřed noci nebo brzy ráno",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "need_bathroom", "type": "choice", "label": "7. Musíte vstávat na toaletu",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "breathing_discomfort", "type": "choice", "label": "8. Nemůžete dýchat pohodlně",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "cough_or_snore", "type": "choice", "label": "9. Kašlete nebo chrápete hlasitě",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "feel_cold", "type": "choice", "label": "10. Cítíte chlad",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "feel_hot", "type": "choice", "label": "11. Cítíte horko",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "bad_dreams", "type": "choice", "label": "12. Míváte nepříjemné sny",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "pain", "type": "choice", "label": "13. Pociťujete bolest",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "other_sleep_difficulty", "type": "textarea", "label": "14. Jiné důvody obtíží – uveďte:", "optional": True},
            {"id": "sleep_medication", "type": "choice", "label": "15. Užíváte léky na spaní?",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "daytime_stay_awake_problems", "type": "choice", "label": "16. Problémy s udržením bdělosti během dne?",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "daytime_activity_attention_difficulty", "type": "choice", "label": "17. Těžko být aktivní a soustředěný přes den?",
             "options": [{"label": "0 – Vůbec ne", "value": 0}, {"label": "1 – Méně než 1× týdně", "value": 1}, {"label": "2 – 1–2× týdně", "value": 2}, {"label": "3 – 3× týdně nebo více", "value": 3}]},
            {"id": "subjective_sleep_quality", "type": "choice", "label": "18. Jak hodnotíte celkovou kvalitu spánku?",
             "options": [{"label": "0 – Velmi dobrá", "value": 0}, {"label": "1 – Celkem dobrá", "value": 1}, {"label": "2 – Celkem špatná", "value": 2}, {"label": "3 – Velmi špatná", "value": 3}]},
        ]
    },
    "mfi20": {
        "title": "MFI-20 – Multidimensional Fatigue Inventory",
        "description": "Vyplňte 1× týdně. U každého tvrzení označte, do jaké míry s ním souhlasíte.",
        "frequency": "weekly",
        "icon": "activity",
        "color": "orange",
        "questions": [
            {"id": "study_week", "type": "number", "label": "Týden studie č.", "min": 1, "max": 52},
            {"id": "gf1", "type": "scale5", "label": "Cítím se unavený/á.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "gf2", "type": "scale5", "label": "Celkově se necítím příliš svěže.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "gf3", "type": "scale5", "label": "Cítím se svěží.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "gf4", "type": "scale5", "label": "Cítím se ve formě.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "pf1", "type": "scale5", "label": "Fyzicky se cítím unavený/á.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "pf2", "type": "scale5", "label": "Fyzicky se cítím vyčerpaný/á.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "pf3", "type": "scale5", "label": "Cítím se silný/á.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "pf4", "type": "scale5", "label": "Cítím se ve fyzicky dobré kondici.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "ra1", "type": "scale5", "label": "Mám málo zájmů.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "ra2", "type": "scale5", "label": "Dělám jen málo věcí.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "ra3", "type": "scale5", "label": "Jsem schopen/na vykonávat spoustu věcí.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "ra4", "type": "scale5", "label": "Provozuji mnoho činností.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "rm1", "type": "scale5", "label": "Nejsem příliš motivovaný/á.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "rm2", "type": "scale5", "label": "Rád/a dělám věci.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "rm3", "type": "scale5", "label": "Musím se nutit k tomu, abych něco dělal/a.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "rm4", "type": "scale5", "label": "Chybí mi motivace.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "mf1", "type": "scale5", "label": "Mám potíže se soustředěním.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "mf2", "type": "scale5", "label": "Je pro mě obtížné myslet jasně.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "mf3", "type": "scale5", "label": "Myslí mi to.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
            {"id": "mf4", "type": "scale5", "label": "Je pro mě obtížné se soustředit.", "options": ["1 – zcela nesouhlasím","2 – spíše nesouhlasím","3 – ani souhlas, ani nesouhlas","4 – spíše souhlasím","5 – zcela souhlasím"]},
        ]
    },
    "tavns_adverse": {
        "title": "Nežádoucí účinky – tAVNS",
        "description": "Vyplňte 1× týdně. Uveďte, zda jste za uplynulý týden zaznamenal/a jakékoliv nežádoucí účinky.",
        "frequency": "weekly",
        "icon": "alert-triangle",
        "color": "red",
        "questions": [
            {"id": "study_week", "type": "number", "label": "Týden studie č.", "min": 1, "max": 52},
            {"id": "local_reaction", "type": "choice", "label": "Lokální reakce v místě aplikace (zarudnutí, podráždění kůže, bolest)",
             "options": [{"label": "Žádné", "value": "none"}, {"label": "Mírné", "value": "mild"}, {"label": "Střední", "value": "moderate"}, {"label": "Závažné", "value": "severe"}]},
            {"id": "local_reaction_desc", "type": "textarea", "label": "Popis lokální reakce:", "optional": True},
            {"id": "neurological_symptoms", "type": "choice", "label": "Neurologické symptomy (bolest hlavy, závratě, parestezie)",
             "options": [{"label": "Žádné", "value": "none"}, {"label": "Mírné", "value": "mild"}, {"label": "Střední", "value": "moderate"}, {"label": "Závažné", "value": "severe"}]},
            {"id": "neurological_desc", "type": "textarea", "label": "Popis neurologických symptomů:", "optional": True},
            {"id": "cardiovascular_symptoms", "type": "choice", "label": "Kardiovaskulární symptomy (palpitace, arytmie, změny TK)",
             "options": [{"label": "Žádné", "value": "none"}, {"label": "Mírné", "value": "mild"}, {"label": "Střední", "value": "moderate"}, {"label": "Závažné", "value": "severe"}]},
            {"id": "cardiovascular_desc", "type": "textarea", "label": "Popis kardiovaskulárních symptomů:", "optional": True},
            {"id": "gastrointestinal_symptoms", "type": "choice", "label": "Gastrointestinální symptomy (nauzea, změny motility)",
             "options": [{"label": "Žádné", "value": "none"}, {"label": "Mírné", "value": "mild"}, {"label": "Střední", "value": "moderate"}, {"label": "Závažné", "value": "severe"}]},
            {"id": "gastrointestinal_desc", "type": "textarea", "label": "Popis GI symptomů:", "optional": True},
            {"id": "non_specific_symptoms", "type": "choice", "label": "Nespecifické symptomy (únava, změny nálady)",
             "options": [{"label": "Žádné", "value": "none"}, {"label": "Mírné", "value": "mild"}, {"label": "Střední", "value": "moderate"}, {"label": "Závažné", "value": "severe"}]},
            {"id": "non_specific_desc", "type": "textarea", "label": "Popis nespecifických symptomů:", "optional": True},
            {"id": "other_adverse_events", "type": "textarea", "label": "Jiné nežádoucí účinky:", "optional": True},
            {"id": "contacted_physician", "type": "choice", "label": "Kontaktoval/a jste výzkumného lékaře?",
             "options": [{"label": "Ne", "value": "no"}, {"label": "Ano", "value": "yes"}]},
        ]
    },
    "meq": {
        "title": "MEQ – Dotazník ranních a večerních typů",
        "description": "Vyplňte JEDNOU na začátku studie.",
        "frequency": "once_start",
        "icon": "sun",
        "color": "amber",
        "questions": [
            {"id": "meq_01", "type": "choice", "label": "1. Kdy byste ráno vstával/a, pokud byste se mohl/a svobodně rozhodnout?",
             "options": [{"label": "5:00–6:30", "value": "a"},{"label": "6:30–7:45", "value": "b"},{"label": "7:45–9:45", "value": "c"},{"label": "Po 9:45", "value": "d"}]},
            {"id": "meq_02", "type": "choice", "label": "2. Kdybyste si večer mohl/a svobodně vybrat čas ke spánku?",
             "options": [{"label": "20:00–21:00", "value": "a"},{"label": "21:00–22:15", "value": "b"},{"label": "22:15–0:30", "value": "c"},{"label": "0:30–2:00", "value": "d"}]},
            {"id": "meq_03", "type": "choice", "label": "3. Jak jste po probuzení v první půlhodině fyzicky aktivní?",
             "options": [{"label": "Velmi aktivní", "value": "a"},{"label": "Poměrně aktivní", "value": "b"},{"label": "Spíše malá aktivita", "value": "c"},{"label": "Velmi málo aktivní", "value": "d"}]},
            {"id": "meq_04", "type": "choice", "label": "4. Kdy byste měl/a nejlepší fyzický výkon?",
             "options": [{"label": "8:00–10:00", "value": "a"},{"label": "11:00–13:00", "value": "b"},{"label": "15:00–17:00", "value": "c"},{"label": "19:00–21:00", "value": "d"}]},
            {"id": "meq_05", "type": "choice", "label": "5. Kdy se večer obvykle cítíte unavený/á?",
             "options": [{"label": "Okolo 21:00", "value": "a"},{"label": "Okolo 22:15", "value": "b"},{"label": "Okolo 23:30", "value": "c"},{"label": "Po půlnoci", "value": "d"}]},
            {"id": "meq_06", "type": "choice", "label": "6. Jak snadno ráno vstáváte?",
             "options": [{"label": "Velmi snadno", "value": "a"},{"label": "Snadno", "value": "b"},{"label": "Těžce", "value": "c"},{"label": "Velmi těžce", "value": "d"}]},
            {"id": "meq_07", "type": "choice", "label": "7. Pokud se ráno musíte vzbudit v 6:00, jak se cítíte?",
             "options": [{"label": "Čerstvě", "value": "a"},{"label": "Poměrně čerstvě", "value": "b"},{"label": "Poměrně unaveně", "value": "c"},{"label": "Velmi unaveně", "value": "d"}]},
            {"id": "meq_08", "type": "choice", "label": "8. Pokud se ráno probudíte bez budíku, v kolik obvykle vstáváte?",
             "options": [{"label": "Před 6:30", "value": "a"},{"label": "6:30–7:45", "value": "b"},{"label": "7:45–9:45", "value": "c"},{"label": "Po 9:45", "value": "d"}]},
            {"id": "meq_09", "type": "choice", "label": "9. Máte rádi brzké vstávání?",
             "options": [{"label": "Ano, velmi", "value": "a"},{"label": "Poměrně ano", "value": "b"},{"label": "Spíše ne", "value": "c"},{"label": "Vůbec ne", "value": "d"}]},
            {"id": "meq_10", "type": "choice", "label": "10. Jakou máte chuť k jídlu v první půlhodině po probuzení?",
             "options": [{"label": "Velkou", "value": "a"},{"label": "Poměrně velkou", "value": "b"},{"label": "Malou", "value": "c"},{"label": "Žádnou", "value": "d"}]},
            {"id": "meq_11", "type": "choice", "label": "11. Rychle se ráno probouzíte a začnete fungovat?",
             "options": [{"label": "Ano, okamžitě", "value": "a"},{"label": "Poměrně rychle", "value": "b"},{"label": "Spíše pomalu", "value": "c"},{"label": "Velmi pomalu", "value": "d"}]},
            {"id": "meq_12", "type": "choice", "label": "12. Kdy byste byl/a nejvíce duševně výkonný/á?",
             "options": [{"label": "8:00–10:00", "value": "a"},{"label": "11:00–13:00", "value": "b"},{"label": "15:00–17:00", "value": "c"},{"label": "19:00–21:00", "value": "d"}]},
            {"id": "meq_13", "type": "choice", "label": "13. Pokud byste musel/a být aktivní mezi 23:00 a 1:00, jak by vám to šlo?",
             "options": [{"label": "Velmi špatně", "value": "a"},{"label": "Poměrně špatně", "value": "b"},{"label": "Poměrně dobře", "value": "c"},{"label": "Velmi dobře", "value": "d"}]},
            {"id": "meq_14", "type": "choice", "label": "14. Pokud byste šel/šla spát ve 1:00, jak byste se cítil/a ráno?",
             "options": [{"label": "Velmi unaveně", "value": "a"},{"label": "Poměrně unaveně", "value": "b"},{"label": "Poměrně čerstvě", "value": "c"},{"label": "Velmi čerstvě", "value": "d"}]},
            {"id": "meq_15", "type": "choice", "label": "15. Kdybyste měl/a 2 hodiny intenzivní práce, kdy byste je zvolil/a?",
             "options": [{"label": "8:00–10:00", "value": "a"},{"label": "11:00–13:00", "value": "b"},{"label": "15:00–17:00", "value": "c"},{"label": "19:00–21:00", "value": "d"}]},
            {"id": "meq_16", "type": "choice", "label": "16. Pokud byste měl/a důležitou zkoušku v 7:00, jak byste na tom byl/a?",
             "options": [{"label": "Velmi dobře", "value": "a"},{"label": "Poměrně dobře", "value": "b"},{"label": "Poměrně špatně", "value": "c"},{"label": "Velmi špatně", "value": "d"}]},
            {"id": "meq_17", "type": "choice", "label": "17. Jak často se budíte před budíkem?",
             "options": [{"label": "Skoro vždy", "value": "a"},{"label": "Často", "value": "b"},{"label": "Občas", "value": "c"},{"label": "Zřídka", "value": "d"}]},
            {"id": "meq_18", "type": "choice", "label": "18. Jste spíše ranní nebo večerní typ?",
             "options": [{"label": "Výrazně ranní", "value": "a"},{"label": "Ranní", "value": "b"},{"label": "Večerní", "value": "c"},{"label": "Výrazně večerní", "value": "d"}]},
            {"id": "meq_19", "type": "choice", "label": "19. Kdybyste si vybral/a jen jeden denní čas pro důležitou práci?",
             "options": [{"label": "8:00–10:00", "value": "a"},{"label": "11:00–13:00", "value": "b"},{"label": "15:00–17:00", "value": "c"},{"label": "19:00–21:00", "value": "d"}]},
        ]
    },
    "tavns_satisfaction": {
        "title": "Dotazník spokojenosti – závěrečný",
        "description": "Vyplňte na KONCI studie.",
        "frequency": "once_end",
        "icon": "star",
        "color": "green",
        "questions": [
            {"id": "overall_experience", "type": "scale5", "label": "Jak celkově hodnotíte vaši účast v této studii?",
             "options": ["1 – Velmi negativně","2","3 – Neutrálně","4","5 – Velmi pozitivně"]},
            {"id": "work_life_acceptability", "type": "scale5", "label": "Jak přijatelný byl protokol stimulace pro váš pracovní život?",
             "options": ["1 – Zcela nepřijatelný","2","3 – Neutrálně","4","5 – Zcela přijatelný"]},
            {"id": "device_usability", "type": "scale5", "label": "Jak snadné bylo používání přístroje Nurosym?",
             "options": ["1 – Velmi obtížné","2","3 – Neutrálně","4","5 – Velmi snadné"]},
            {"id": "questionnaire_burden", "type": "scale5", "label": "Jak hodnotíte zátěž spojenou s vyplňováním dotazníků?",
             "options": ["1 – Velmi zatěžující","2","3 – Neutrálně","4","5 – Velmi snadné"]},
            {"id": "fatigue_sleep_change", "type": "choice", "label": "Zaznamenal/a jste subjektivní zlepšení únavy nebo spánku?",
             "options": [{"label": "Výrazné zlepšení", "value": "significant_improvement"},{"label": "Mírné zlepšení", "value": "mild_improvement"},{"label": "Beze změny", "value": "no_change"},{"label": "Mírné zhoršení", "value": "mild_worsening"},{"label": "Výrazné zhoršení", "value": "significant_worsening"}]},
            {"id": "recommend_to_colleagues", "type": "choice", "label": "Doporučil/a byste účast v podobné studii kolegům?",
             "options": [{"label": "Určitě ano", "value": "definitely_yes"},{"label": "Spíše ano", "value": "rather_yes"},{"label": "Spíše ne", "value": "rather_no"},{"label": "Určitě ne", "value": "definitely_no"}]},
            {"id": "continue_stimulation", "type": "choice", "label": "Pokud by to bylo možné, chtěl/a byste pokračovat ve stimulaci?",
             "options": [{"label": "Určitě ano", "value": "definitely_yes"},{"label": "Spíše ano", "value": "rather_yes"},{"label": "Spíše ne", "value": "rather_no"},{"label": "Určitě ne", "value": "definitely_no"}]},
            {"id": "liked_most", "type": "textarea", "label": "Co se vám na studii líbilo nejvíce?", "optional": True},
            {"id": "improvements", "type": "textarea", "label": "Co by se dalo zlepšit?", "optional": True},
            {"id": "additional_comments", "type": "textarea", "label": "Jakékoliv další komentáře:", "optional": True},
        ]
    },
    "pvt": {
        "title": "PVT – Reakční test bdělosti",
        "description": "Psychomotorický vigilanční test. 10 pokusů, ~3 minuty. Výsledek se ukládá automaticky.",
        "frequency": "per_shift_after",
        "icon": "zap",
        "color": "cyan",
        "questions": [
            {"id": "mean_rt",     "type": "number", "label": "Průměrná reakční doba (ms)"},
            {"id": "min_rt",      "type": "number", "label": "Nejrychlejší reakce (ms)"},
            {"id": "lapses",      "type": "number", "label": "Lapsy (reakce > 500 ms)"},
            {"id": "false_starts","type": "number", "label": "Falešné starty"},
            {"id": "n_trials",    "type": "number", "label": "Počet pokusů"},
            {"id": "trials",      "type": "text",   "label": "Surová data (JSON pole ms)", "optional": True},
        ]
    },
    "stim_done": {
        "title": "tAVNS stimulace provedena",
        "description": "Potvrzení provedené udržovací stimulace.",
        "frequency": "daily",
        "icon": "zap",
        "color": "violet",
        "questions": []
    },
    'stim_pre': {
        'title': 'tAVNS stimulace (před směnou)',
        'description': 'Stimulace před zahájením noční směny (15 min)',
        'questions': [],
        'frequency': 'daily',
    },
    'stim_p1': {
        'title': 'tAVNS stimulace (pauza 1)',
        'description': 'Stimulace v pauze ~21:00 (5 min)',
        'questions': [],
        'frequency': 'daily',
    },
    'stim_p2': {
        'title': 'tAVNS stimulace (pauza 2)',
        'description': 'Stimulace v pauze ~00:00 (5 min)',
        'questions': [],
        'frequency': 'daily',
    },
    'stim_p3': {
        'title': 'tAVNS stimulace (pauza 3)',
        'description': 'Stimulace v pauze ~03:00 (5 min)',
        'questions': [],
        'frequency': 'daily',
    },
    'stim_end': {
        'title': 'tAVNS stimulace (závěrečná)',
        'description': 'Závěrečná stimulace před koncem noční směny (15 min)',
        'questions': [],
        'frequency': 'daily',
    },
    "cortizol_done": {
        "title": "Odběr kortizolu proveden",
        "description": "Potvrzení odběru kortizolu (slin) v kortizolový den studie.",
        "frequency": "daily",
        "icon": "droplet",
        "color": "purple",
        "questions": []
    },
}

class ResponseCreate(BaseModel):
    q_type: str
    shift_id: Optional[int] = None
    answers: dict
    duration_seconds: Optional[int] = None

@router.get("/definitions")
def get_definitions():
    return QUESTIONNAIRE_DEFINITIONS

@router.get("/definitions/{q_type}")
def get_definition(q_type: str):
    if q_type not in QUESTIONNAIRE_DEFINITIONS:
        raise HTTPException(404, "Dotaznik nenalezen")
    return QUESTIONNAIRE_DEFINITIONS[q_type]

@router.post("/")
def submit_response(
    data: ResponseCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if data.q_type not in QUESTIONNAIRE_DEFINITIONS:
        raise HTTPException(400, "Neznamy typ dotazniku")
    resp = QuestionnaireResponse(
        user_id=current_user.id,
        shift_id=data.shift_id,
        q_type=data.q_type,
        answers=json.dumps(data.answers, ensure_ascii=False),
        phase=current_user.phase,
        duration_seconds=data.duration_seconds,
    )
    db.add(resp)
    db.commit()
    db.refresh(resp)
    return {"id": resp.id, "filled_at": utc_iso(resp.filled_at)}

@router.get("/my")
def my_responses(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    responses = db.query(QuestionnaireResponse)\
                  .filter(QuestionnaireResponse.user_id == current_user.id)\
                  .order_by(QuestionnaireResponse.filled_at.desc()).limit(500).all()
    return [
        {
            "id": r.id,
            "q_type": r.q_type,
            "shift_id": r.shift_id,
            "filled_at": utc_iso(r.filled_at),
            "answers": json.loads(r.answers),
        }
        for r in responses
    ]

@router.delete("/{response_id}")
def delete_response(response_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.query(QuestionnaireResponse).filter(QuestionnaireResponse.id == response_id).first()
    if not r:
        raise HTTPException(404, "Záznam nenalezen")
    if current_user.role not in ("admin", "researcher") and r.user_id != current_user.id:
        raise HTTPException(403, "Nemáte oprávnění")
    db.delete(r)
    db.commit()
    return {"ok": True}

@router.patch("/{response_id}")
def update_response(response_id: int, body: dict, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.query(QuestionnaireResponse).filter(QuestionnaireResponse.id == response_id).first()
    if not r:
        raise HTTPException(404, "Záznam nenalezen")
    if current_user.role not in ("admin", "researcher") and r.user_id != current_user.id:
        raise HTTPException(403, "Nemáte oprávnění")
    if "answers" in body:
        r.answers = json.dumps(body["answers"], ensure_ascii=False)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "answers": json.loads(r.answers)}

@router.get("/user/{user_id}", dependencies=[Depends(require_researcher)])
def user_responses(user_id: int, db: Session = Depends(get_db)):
    responses = db.query(QuestionnaireResponse)\
                  .filter(QuestionnaireResponse.user_id == user_id)\
                  .order_by(QuestionnaireResponse.filled_at.desc()).limit(500).all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "q_type": r.q_type,
            "phase": r.phase,
            "filled_at": utc_iso(r.filled_at),
            "answers": json.loads(r.answers),
        }
        for r in responses
    ]

@router.get("/all", dependencies=[Depends(require_researcher)])
def all_responses(db: Session = Depends(get_db)):
    responses = db.query(QuestionnaireResponse)\
                  .order_by(QuestionnaireResponse.filled_at.desc()).limit(500).all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "q_type": r.q_type,
            "shift_id": r.shift_id,
            "phase": r.phase,
            "filled_at": utc_iso(r.filled_at),
            "answers": json.loads(r.answers),
        }
        for r in responses
    ]
