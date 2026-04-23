import os
import io
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from dotenv import load_dotenv

from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, DateTime, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import pandas as pd

load_dotenv()

# Инициализация хранилища
if not os.path.exists('data'): os.makedirs('data')

# --- АВТООЧИСТКА (РОТАЦИЯ) ЛОГОВ ---
log_path = 'data/escalations.log'
handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=1, encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
logger = logging.getLogger("TelecomLogger")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

Base = declarative_base()
engine = create_engine(os.getenv("DATABASE_URL"), connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()


class TicketState(Base):
    __tablename__ = 'active_tickets'
    msisdn = Column(String, primary_key=True)
    ticket_id = Column(String)
    theme = Column(String, primary_key=True) # Составной ключ для уникальности комбинации
    subject = Column(String, primary_key=True)
    product = Column(String, primary_key=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)
app = FastAPI(title="Interceptor")


class PHPRequest(BaseModel):
    ID: str
    MSISDN: str
    DESCR: str
    SUBJECT_NAME: str
    PRODUCT_NAME: str


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username == os.getenv("ADMIN_USERNAME") and credentials.password == os.getenv("ADMIN_PASSWORD"):
        return "admin"
    raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


@app.post("/process")
async def process_ticket(data: PHPRequest):
    db = SessionLocal()
    now = datetime.utcnow()
    descr_text = data.DESCR.lower().strip() if data.DESCR else ""

    # --- ПРИНУДИТЕЛЬНАЯ ЭСКАЛАЦИЯ, ЕСЛИ РЕКОМНЕДАЦИИ БЫЛИ ВЫПОЛНЕНЫ ---
    bypass_phrases = ["повтор", "рекоменд", "снова", "ашылғ", "көмек", "қайтад"]
    if any(phrase in descr_text for phrase in bypass_phrases):
        logger.info(f"MANUAL_BYPASS | ID: {data.ID} | MSISDN: {data.MSISDN} | User made 1st recommend")
        db.close()
        return {"action": "manual_processing", "target": "L2-SCU"}

    # 1. СПИСКИ РАЗРЕШЕННЫХ SUBJECT_NAME И PRODUCT_NAME
    ALLOWED_SUBJECTS = [
        "here your SUBJECTS"
    ]
    ALLOWED_PRODUCTS = [
        "here your PRODUCTS"
    ]

    # 2. ОПРЕДЕЛЕНИЕ TARGET_THEME ПО DESCR
    INTERNET_REPLY = (
        "1.Попросите абонента: Перезагрузить устройство, отключить Wi-Fi, проверить точку доступа (APN).\n"
        "2.Смена режима: Переключить сеть 3G/4G/5G вручную.\n"
        "3.Эскалация: Если проблема носит локальный характер направьте в CQ."
    )

    VOICE_REPLY = (
        "1.Тестовые вызовы: Сделайте входящий звонок на номер абонента (А) с тестового номера. L1"
        "2.Затем попросите абонента совершить ответный звонок на номер L1.\n"
        " - Если звонки прошли в обе стороны: Уведомите, что услуга работает корректно.\n"
        " - Если проблема с одним номером (Б): Попробуйте набрать номер Б с L1. Если не проходит — проблема на стороне Б.\n"
        " - Если 3-4 гудка и сброс: Объясните абоненту, что это тайм-аут ожидания (20 сек).\n\n"
        "3.Техническая настройка: Если наборы не проходят, попросите абонента:\n"
        " - Перезагрузить устройство.\n"
        " - Включен режим 'Не беспокоить' или активирована блокировка вызова от неизвестных номеров.\n"
        " - Отключить Wi-Fi, оставить одну SIM-карту в слоте.\n"
        " - Сменить режим сети 2G/3G/4G.\n\n"
        "4.Эскалация: Если наборы не прошли после настроек — ОТКРЫВАЙТЕ НОВЫЙ ТИКЕТ.\n"
        "   ОБЯЗАТЕЛЬНО: Укажите точную дату, время, номер Б и адрес нахождения абонента."
    )

    found_rec = None
    target_theme = None

    is_internet = any(word in descr_text for word in ["интернет", "инет", "интер", "internet", "данные", "3g", "4g", "5g", "3г", "4г", "5г"])
    is_voice = any(word in descr_text for word in ["голос", "звон", "связь", "вызов", "исх", "вход", "вх", "вхд", "набор"])

    # Если в тексте есть И ГОЛОС, И ИНТЕРНЕТ
    if is_voice and is_internet:
        # Даем расширенную голосовую инструкцию (она приоритетнее)
        found_rec = VOICE_REPLY + "\n\n⚠️ ТАКЖЕ: По интернету проверьте APN и режим сети 4G/5G."
        target_theme = "Голосовые услуги"  # Основная категория для статистики

    # Если ТОЛЬКО ГОЛОС
    elif is_voice:
        found_rec = VOICE_REPLY
        target_theme = "Голосовые услуги"

    # Если ТОЛЬКО ИНТЕРНЕТ
    elif is_internet:
        found_rec = INTERNET_REPLY
        target_theme = "Internet"


    # 3. ПРАВИЛО ИСКЛЮЧЕНИЯ (Если метаданные или тема не в списке -> сразу в L2 SCU)
    is_valid_meta = (data.SUBJECT_NAME in ALLOWED_SUBJECTS and data.PRODUCT_NAME in ALLOWED_PRODUCTS)

    if not is_valid_meta or not target_theme:
        logger.info(f"MANUAL_MODE | ID: {data.ID} | MSISDN: {data.MSISDN} | Reason: Metadata or Theme mismatch")
        db.close()
        return {"action": "manual_processing", "target": "L2-SCU"}

    # 4. ПРОВЕРКА ПОВТОРА (Совпадение всех 4-х параметров за 7 дней)
    record = db.query(TicketState).filter(
        TicketState.msisdn == data.MSISDN,
        TicketState.theme == target_theme,
        TicketState.subject == data.SUBJECT_NAME,
        TicketState.product == data.PRODUCT_NAME
    ).filter(TicketState.updated_at >= (now - timedelta(days=7))).first()

    if record:
        # ПОВТОР -> На L2 SCU
        logger.info(f"SILENT_ESC | ID: {data.ID} | MSISDN: {data.MSISDN} | Theme: {target_theme} | Status: Repeat")
        db.close()
        return {"action": "silent_escalate", "target": "L2-SCU"}

    # 5. ПЕРВИЧНОЕ ОБРАЩЕНИЕ -> Autoreply и ЗАКРЫТИЕ на стороне PHP
    new_entry = TicketState(
        msisdn=data.MSISDN,
        ticket_id=data.ID,
        theme=target_theme,
        subject=data.SUBJECT_NAME,
        product=data.PRODUCT_NAME,
        updated_at=now
    )
    db.merge(new_entry)
    db.commit()
    db.close()

    logger.info(f"AUTOREPLY | ID: {data.ID} | MSISDN: {data.MSISDN} | Theme: {target_theme} | Action: Close Ticket")
    return {
        "action": "autoreply",
        "message": found_rec,
        "php_status": "CLOSED"
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, role: str = Depends(authenticate)):
    db = SessionLocal()
    tickets = db.query(TicketState).order_by(TicketState.updated_at.desc()).all()
    logs = []
    if os.path.exists(log_path):
        # Добавляем errors='replace', чтобы не падать на кодировке
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            logs = f.readlines()[-25:]
    db.close()
    return templates.TemplateResponse("admin.html", {"request": request, "tickets": tickets, "logs": logs})


@app.get("/admin/export")
async def export_excel(role: str = Depends(authenticate)):
    db = SessionLocal()
    query = db.query(TicketState).all()
    data = []
    for t in query:
        data.append({
            "MSISDN": t.msisdn,
            "Ticket ID": t.ticket_id,
            "Theme": t.theme,
            "Subject": t.subject,
            "Product": t.product,
            "Date": t.updated_at
        })
    df = pd.DataFrame(data)
    db.close()
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as w: df.to_excel(w, index=False)
    out.seek(0)
    return StreamingResponse(out, headers={"Content-Disposition": "attachment; filename=report.xlsx"},
                             media_type="application/vnd.ms-excel")
