import os
import json
import asyncio
import logging
import aiohttp
from dotenv import load_dotenv
from typing import Dict, Set

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# YooKassa (без вебхуков — будем опрашивать статус)
from yookassa import Configuration, Payment

from database import db  # ваш модуль Database с глобальным экземпляром db

# ──────────────────────────── Настройка ───────────────────────────────
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("TOKEN не найден в .env")

# KIE
KIE_API_BASE = os.getenv("KIE_API_BASE", "https://api.kie.ai")
KIE_API_KEY = os.getenv("KIE_API_KEY")
if not KIE_API_KEY:
    raise ValueError("KIE_API_KEY не найден в .env")
JOBS_CREATE = f"{KIE_API_BASE}/api/v1/jobs/createTask"
JOBS_STATUS = f"{KIE_API_BASE}/api/v1/jobs/recordInfo"

# YooKassa
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/your_bot_username")
if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
    logging.warning("YooKassa не настроена: нет YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY")

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Храним id последнего инвойса Stars на пользователя, чтобы удалить его после оплаты
LAST_INVOICE_MSG: Dict[int, int] = {}
# Fallback-набор, если в БД нет идемпотентного метода
APPLIED_CHARGES: Set[str] = set()

# ──────────────────────────── Состояния ───────────────────────────────
class VideoCreationStates(StatesGroup):
    waiting_for_prompt_type = State()
    waiting_for_model_tier = State()
    waiting_for_quality = State()               # только для Pro
    waiting_for_duration_orientation = State()  # длительность + ориентация
    waiting_for_image = State()
    waiting_for_prompt = State()
    waiting_for_confirmation = State()

class BalanceStates(StatesGroup):
    waiting_for_payment_method = State()

# ──────────────────────────── Цены генераций ──────────────────────────
def calc_cost_credits(tier: str, quality: str | None, duration: int) -> int:
    """
    Цены:
    - Sora 2:             10s → 30,   15s → 35
    - Sora 2 Pro Standard 10s → 90,   15s → 135
    - Sora 2 Pro HD       10s → 200,  15s → 400
    """
    if tier == "sora2":
        return 30 if duration == 10 else 35
    if tier == "sora2_pro":
        if quality == "high":
            return 200 if duration == 10 else 400
        return 90 if duration == 10 else 135
    return 30 if duration == 10 else 35

def duration_price_text(tier: str | None, quality: str | None) -> str:
    if not tier:
        return "Выберите длительность и ориентацию:"
    if tier == "sora2":
        return (
            "Выберите длительность и ориентацию:\n\n"
            "🧠 *Sora 2*: 10 с — *30* токенов, 15 с — *35* токенов"
        )
    if tier == "sora2_pro" and (not quality or quality == "std"):
        return (
            "Выберите длительность и ориентацию:\n\n"
            "⚡ *Sora 2 Pro (Standard)*: 10 с — *90* токенов, 15 с — *135* токенов"
        )
    if tier == "sora2_pro" and quality == "high":
        return (
            "Выберите длительность и ориентацию:\n\n"
            "💎 *Sora 2 Pro (HD)*: 10 с — *200* токенов, 15 с — *400* токенов"
        )
    return "Выберите длительность и ориентацию:"

# ──────────────────────────── Клавиатуры ──────────────────────────────
def back_btn(data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🔙 Назад", callback_data=data)

def get_reply_keyboard() -> ReplyKeyboardMarkup:
    # Постоянное «нижнее» меню под полем ввода
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Создать видео")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="💳 Пополнить баланс")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие…"
    )

def get_prompt_type_keyboard(selected: str | None = None):
    t2v = "✅ Текст → Видео" if selected == "t2v" else "Текст → Видео"
    i2v = "✅ Фото → Видео" if selected == "i2v" else "Фото → Видео"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t2v, callback_data="ptype_t2v"),
         InlineKeyboardButton(text=i2v, callback_data="ptype_i2v")],
        [back_btn("back_to_main")]
    ])

def get_model_tier_keyboard(selected: str | None = None):
    s2 = "✅ Sora 2" if selected == "sora2" else "Sora 2"
    s2p = "✅ Sora 2 Pro" if selected == "sora2_pro" else "Sora 2 Pro"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s2, callback_data="tier_sora2"),
         InlineKeyboardButton(text=s2p, callback_data="tier_sora2pro")],
        [back_btn("back_to_prompt_type")]
    ])

def get_quality_keyboard(selected: str | None = None):
    std = "✅ Стандарт" if selected == "std" else "Стандарт"
    high = "✅ Высокое" if selected == "high" else "Высокое"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=std, callback_data="qual_std"),
         InlineKeyboardButton(text=high, callback_data="qual_high")],
        [InlineKeyboardButton(text="➡️ Далее", callback_data="quality_next")],
        [back_btn("back_to_model_tier")]
    ])

def get_duration_orientation_keyboard(selected_duration: int | None = None,
                                      selected_orientation: str | None = None):
    d10 = "✅ 10 с" if selected_duration == 10 else "10 с"
    d15 = "✅ 15 с" if selected_duration == 15 else "15 с"
    o916 = "✅ 9:16 (верт.)" if selected_orientation == "9:16" else "9:16"
    o169 = "✅ 16:9 (гор.)" if selected_orientation == "16:9" else "16:9"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=d10, callback_data="duration_10"),
         InlineKeyboardButton(text=d15, callback_data="duration_15")],
        [InlineKeyboardButton(text=o916, callback_data="orientation_9_16"),
         InlineKeyboardButton(text=o169, callback_data="orientation_16_9")],
        [InlineKeyboardButton(text="✅ Продолжить", callback_data="continue_video")],
        [back_btn("back_to_quality_or_tier")]
    ])

def get_confirmation_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_video")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="change_video")],
        [back_btn("back_to_prompt")]
    ])

# ──────────────────────────── Утилиты KIE ─────────────────────────────
def _kie_headers():
    return {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}

def _map_aspect_ratio(o: str) -> str:
    return "portrait" if o.strip() == "9:16" else "landscape"

def _map_n_frames(dur: int) -> str:
    return "15" if int(dur) >= 15 else "10"

def _build_kie_model(ptype: str, tier: str, quality: str | None) -> str:
    # Для Pro качество задаём параметром size, а не в имени модели
    if ptype == "t2v" and tier == "sora2":      return "sora-2-text-to-video"
    if ptype == "i2v" and tier == "sora2":      return "sora-2-image-to-video"
    if ptype == "t2v" and tier == "sora2_pro":  return "sora-2-pro-text-to-video"
    if ptype == "i2v" and tier == "sora2_pro":  return "sora-2-pro-image-to-video"
    return "sora-2-text-to-video"

# ─────────────────────────────── Хэндлеры UI ──────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if not await db.get_user(uid):
        await db.create_user(uid)
    text = (
        "👋 Привет! Я делаю видео с помощью Sora 2.\n\n"
        "1️⃣ Тип: Текст→Видео или Фото→Видео\n"
        "2️⃣ Модель: Sora 2 / Sora 2 Pro (Стандарт/Высокое)\n"
        "3️⃣ Выбери длительность и ориентацию\n"
        "4️⃣ Опиши сцену — и готово!\n\n"
        "💳 Пополнить — внизу (⭐ или 💵). Баланс — «💰 Баланс»."
    )
    await message.answer(text, reply_markup=get_reply_keyboard())

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Нижнее меню включено.", reply_markup=get_reply_keyboard())

# старт из нижнего меню
@dp.message(F.text == "🎬 Создать видео")
async def menu_create_video(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not await db.has_generations(uid):
        await message.answer("❌ У вас нет токенов. Нажмите «💳 Пополнить баланс».")
        return
    await state.set_state(VideoCreationStates.waiting_for_prompt_type)
    await state.update_data(
        prompt_type=None, tier=None, quality=None,
        duration=None, orientation=None,
        image_url=None, prompt=None, cost=None, kie_model=None
    )
    await message.answer("Выберите тип промпта:", reply_markup=get_prompt_type_keyboard())

# назад в «главное»
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🏠 Главное меню. Используйте кнопки внизу.")
    await state.clear()

# выбор типа промпта
@dp.callback_query(F.data.in_({"ptype_t2v", "ptype_i2v"}))
async def choose_prompt_type(callback: CallbackQuery, state: FSMContext):
    ptype = "t2v" if callback.data == "ptype_t2v" else "i2v"
    await state.update_data(prompt_type=ptype)
    await state.set_state(VideoCreationStates.waiting_for_model_tier)
    await callback.message.edit_text("Выберите модель:", reply_markup=get_model_tier_keyboard())

# назад с модели к типу
@dp.callback_query(F.data == "back_to_prompt_type")
async def back_to_prompt_type(callback: CallbackQuery, state: FSMContext):
    await state.set_state(VideoCreationStates.waiting_for_prompt_type)
    await callback.message.edit_text("Выберите тип промпта:", reply_markup=get_prompt_type_keyboard())

# выбор модели
@dp.callback_query(F.data.in_({"tier_sora2", "tier_sora2pro"}))
async def choose_tier(callback: CallbackQuery, state: FSMContext):
    tier = "sora2" if callback.data == "tier_sora2" else "sora2_pro"
    await state.update_data(tier=tier)
    if tier == "sora2_pro":
        await state.set_state(VideoCreationStates.waiting_for_quality)
        await callback.message.edit_text("Выберите качество:", reply_markup=get_quality_keyboard())
    else:
        await state.set_state(VideoCreationStates.waiting_for_duration_orientation)
        await callback.message.edit_text(
            duration_price_text(tier, None),
            reply_markup=get_duration_orientation_keyboard(),
            parse_mode="Markdown"
        )

# назад с качества → модель
@dp.callback_query(F.data == "back_to_model_tier")
async def back_to_model_tier(callback: CallbackQuery, state: FSMContext):
    await state.set_state(VideoCreationStates.waiting_for_model_tier)
    await callback.message.edit_text("Выберите модель:", reply_markup=get_model_tier_keyboard())

# выбор качества (только Pro)
@dp.callback_query(F.data.in_({"qual_std", "qual_high", "quality_next"}))
async def choose_quality(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if callback.data in {"qual_std", "qual_high"}:
        q = "std" if callback.data == "qual_std" else "high"
        await state.update_data(quality=q)
        await callback.message.edit_reply_markup(reply_markup=get_quality_keyboard(selected=q))
        return
    tier, q = data.get("tier"), data.get("quality")
    await state.set_state(VideoCreationStates.waiting_for_duration_orientation)
    await callback.message.edit_text(
        duration_price_text(tier, q),
        reply_markup=get_duration_orientation_keyboard(),
        parse_mode="Markdown"
    )

# назад с длительности/ориентации → качество (или модель)
@dp.callback_query(F.data == "back_to_quality_or_tier")
async def back_to_quality_or_tier(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("tier") == "sora2_pro":
        await state.set_state(VideoCreationStates.waiting_for_quality)
        await callback.message.edit_text("Выберите качество:", reply_markup=get_quality_keyboard(selected=data.get("quality")))
    else:
        await state.set_state(VideoCreationStates.waiting_for_model_tier)
        await callback.message.edit_text("Выберите модель:", reply_markup=get_model_tier_keyboard())

# выбор длительности
@dp.callback_query(F.data.startswith("duration_"))
async def duration_cb(callback: CallbackQuery, state: FSMContext):
    dur = int(callback.data.split("_")[1])
    await state.update_data(duration=dur)
    data = await state.get_data()
    await callback.message.edit_reply_markup(
        reply_markup=get_duration_orientation_keyboard(
            selected_duration=dur,
            selected_orientation=data.get("orientation")
        )
    )

# выбор ориентации
@dp.callback_query(F.data.startswith("orientation_"))
async def orientation_cb(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")  # ["orientation","9","16"]
    o = parts[1] + ":" + parts[2]
    await state.update_data(orientation=o)
    data = await state.get_data()
    await callback.message.edit_reply_markup(
        reply_markup=get_duration_orientation_keyboard(
            selected_duration=data.get("duration"),
            selected_orientation=o
        )
    )

# Далее → картинка или текст
@dp.callback_query(F.data == "continue_video")
async def cont_video(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('duration') or not data.get('orientation'):
        await callback.answer("❌ Выберите длительность и ориентацию!", show_alert=True)
        return

    if data.get("prompt_type") == "i2v":
        await state.set_state(VideoCreationStates.waiting_for_image)
        await callback.message.edit_text(
            "📷 Отправьте изображение (как фото, не файл).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn("back_to_duration")]])
        )
    else:
        await state.set_state(VideoCreationStates.waiting_for_prompt)
        await callback.message.edit_text(
            "✍️ Введите описание для видео:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn("back_to_duration")]])
        )

# назад c prompt/image → к длительности/ориентации
@dp.callback_query(F.data == "back_to_duration")
async def back_to_duration(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tier, q = data.get("tier"), data.get("quality")
    await state.set_state(VideoCreationStates.waiting_for_duration_orientation)
    await callback.message.edit_text(
        duration_price_text(tier, q),
        reply_markup=get_duration_orientation_keyboard(
            selected_duration=data.get("duration"),
            selected_orientation=data.get("orientation")
        ),
        parse_mode="Markdown"
    )

# изображение (I2V)
@dp.message(VideoCreationStates.waiting_for_image, F.photo)
async def got_image(message: types.Message, state: FSMContext):
    ph = message.photo[-1]
    file = await bot.get_file(ph.file_id)
    img_url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
    await state.update_data(image_url=img_url)
    await state.set_state(VideoCreationStates.waiting_for_prompt)
    await message.answer(
        "✍️ Добавьте описание.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn("back_to_duration")]])
    )

@dp.message(VideoCreationStates.waiting_for_image)
async def got_not_image(message: types.Message, state: FSMContext):
    await message.answer("Пожалуйста, отправьте картинку как _фото_, не файлом.", parse_mode="Markdown")

# промпт (общий T2V/I2V)
@dp.message(VideoCreationStates.waiting_for_prompt)
async def prompt_msg(message: types.Message, state: FSMContext):
    prompt = message.text
    await state.update_data(prompt=prompt)

    data = await state.get_data()
    model = _build_kie_model(data.get("prompt_type"), data.get("tier"), data.get("quality"))
    cost = calc_cost_credits(data.get("tier"), data.get("quality"), data.get("duration"))
    await state.update_data(kie_model=model, cost=cost)

    tier_human = "Sora 2 Pro" if data.get("tier") == "sora2_pro" else "Sora 2"
    quality_human = ""
    if data.get("tier") == "sora2_pro":
        quality_human = " (HD)" if data.get("quality") == "high" else " (Standard)"
    mode_human = "Text→Video" if data.get("prompt_type") == "t2v" else "Image→Video"

    info = [
        "⏳ Генерация может занять до 15 минут."
        "📋 Подтвердите параметры:",
        f"Тип: {mode_human}",
        f"Модель: {tier_human}{quality_human}",
        f"Длительность: {data['duration']} с",
        f"Ориентация: {data.get('orientation')}",
        f"💳 Стоимость: {cost} токенов",
        "",
        f"📝 {prompt}"
    ]
    await message.answer("\n".join(info), reply_markup=get_confirmation_keyboard())

# назад с подтверждения → prompt
@dp.callback_query(F.data == "back_to_prompt")
async def back_to_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(VideoCreationStates.waiting_for_prompt)
    await callback.message.edit_text(
        "✍️ Измените описание:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn("back_to_duration")]])
    )

# «Изменить» → вернуться к длительности/ориентации
@dp.callback_query(F.data == "change_video")
async def change_video(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tier, q = data.get("tier"), data.get("quality")
    await state.set_state(VideoCreationStates.waiting_for_duration_orientation)
    await callback.message.edit_text(
        duration_price_text(tier, q),
        reply_markup=get_duration_orientation_keyboard(
            selected_duration=data.get("duration"),
            selected_orientation=data.get("orientation")
        ),
        parse_mode="Markdown"
    )

# подтверждение → проверка баланса, списание, запуск
@dp.callback_query(F.data == "confirm_video")
async def confirm_video(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = callback.from_user.id
    cost = int(data.get("cost") or 0)
    user = await db.get_user(uid)

    if not user or user["generations_left"] < cost:
        bal = user["generations_left"] if user else 0
        await callback.message.edit_text(
            f"❌ Недостаточно токенов.\nНужно {cost}, у вас {bal}."
        )
        await state.clear()
        return

    # списание ровно cost
    await db.update_user_generations(uid, user["generations_left"] - cost)
    await callback.message.edit_text(f"🎬 Видео создаётся…\n💳 Списано {cost} токенов.")

    try:
        await send_to_kie_api(
            uid,
            data["kie_model"],
            data["prompt"],
            data["duration"],
            data.get("orientation"),
            data.get("image_url"),
            cost,
            data.get("tier"),
            data.get("quality"),
            data.get("prompt_type")
        )
    except Exception:
        await db.add_generations(uid, cost)  # возврат при исключении
    finally:
        await state.clear()

# ─────────────── Баланс и пополнение ────────────────
@dp.message(F.text == "💰 Баланс")
async def menu_check_balance(message: Message):
    uid = message.from_user.id
    user = await db.get_user(uid)
    txt = f"💰 Ваш баланс:\n\n🪙 Токенов: {user['generations_left']}" if user else "❌ Пользователь не найден"
    await message.answer(txt)

@dp.message(F.text == "💳 Пополнить баланс")
async def menu_top_up_balance(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Звёзды", callback_data="pay_stars")],
        [InlineKeyboardButton(text="💵 Рубли (YooKassa)", callback_data="pay_rub")],
        [back_btn("back_to_main")],
    ])
    await message.answer("💳 Выберите способ пополнения:", reply_markup=kb)
    await state.set_state(BalanceStates.waiting_for_payment_method)

@dp.callback_query(F.data == "check_balance")
async def check_balance_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    user = await db.get_user(uid)
    txt = f"💰 Ваш баланс:\n\n🪙 Токенов: {user['generations_left']}" if user else "❌ Пользователь не найден"
    await callback.message.edit_text(txt)

@dp.callback_query(F.data == "top_up_balance")
async def top_up_balance_cb(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Звёзды", callback_data="pay_stars")],
        [InlineKeyboardButton(text="💵 Рубли (YooKassa)", callback_data="pay_rub")],
        [back_btn("back_to_main")]
    ])
    await callback.message.edit_text("💳 Выберите способ пополнения:", reply_markup=kb)
    await state.set_state(BalanceStates.waiting_for_payment_method)

# ──────────────────────────── Команда /get_id ────────────────────────────
@dp.message(Command("get_id"))
async def cmd_get_id(message: types.Message):
    uid = message.from_user.id
    await message.answer(f"🆔 Ваш Telegram ID: <b>{uid}</b>", parse_mode="HTML")


# ──────────────────────────── Команда /give_tokens (для админа) ────────────────────────────
ADMIN_IDS = {683135069}  # ← сюда впиши свой Telegram ID (через запятую, если несколько)

@dp.message(Command("give_tokens"))
async def cmd_give_tokens(message: types.Message):
    # Проверяем, админ ли отправитель
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("⚙️ Использование: <code>/give_tokens user_id amount</code>", parse_mode="HTML")
        return

    try:
        target_id = int(parts[1])
        amount = int(parts[2])
    except ValueError:
        await message.answer("❌ ID и количество должны быть числами.")
        return

    # Проверяем, есть ли такой пользователь в БД
    user = await db.get_user(target_id)
    if not user:
        await message.answer("⚠️ Пользователь с таким ID не найден в базе.")
        return

    # Начисляем токены
    await db.add_generations(target_id, amount)

    # Уведомляем
    await message.answer(f"✅ Пользователю <b>{target_id}</b> начислено <b>{amount}</b> токенов.", parse_mode="HTML")
    try:
        await bot.send_message(target_id, f"🎁 Вам начислено <b>{amount}</b> токенов администратором.", parse_mode="HTML")
    except Exception:
        pass

# ───── Stars: пакеты 20/60/120/300 → 30/100/200/500 токенов ─────
STAR_PACKS = {
    "20":  {"stars": 20,  "tokens": 30,  "title": "⭐ 20 звёзд → 30 токенов"},
    "60":  {"stars": 60,  "tokens": 100, "title": "⭐ 60 звёзд → 100 токенов"},
    "120": {"stars": 120, "tokens": 200, "title": "⭐ 120 звёзд → 200 токенов"},
    "300": {"stars": 300, "tokens": 500, "title": "⭐ 300 звёзд → 500 токенов"},
}

@dp.callback_query(F.data == "pay_stars")
async def pay_stars_cb(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=STAR_PACKS["20"]["title"],   callback_data="stars_20")],
        [InlineKeyboardButton(text=STAR_PACKS["60"]["title"],   callback_data="stars_60")],
        [InlineKeyboardButton(text=STAR_PACKS["120"]["title"],  callback_data="stars_120")],
        [InlineKeyboardButton(text=STAR_PACKS["300"]["title"],  callback_data="stars_300")],
        [back_btn("top_up_balance")]
    ])
    await callback.message.edit_text("⭐ Выберите пакет для пополнения:\nДешево звезды можно купить тут - @cheapiest_star_bot", reply_markup=kb)

@dp.callback_query(F.data.startswith("stars_"))
async def stars_package_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    pack = callback.data.split("_")[1]

    if pack not in STAR_PACKS:
        await callback.answer("❌ Неверный пакет", show_alert=True)
        return

    pkg = STAR_PACKS[pack]

    payload = json.dumps({
        "kind": "stars_pack",
        "pack": pack,
        "stars": pkg["stars"],
        "tokens": pkg["tokens"],
        "uid": uid
    })

    # Stars: provider_token="" и currency="XTR"; prices — ровно один элемент
    prices = [LabeledPrice(label=f"{pkg['stars']} ⭐", amount=pkg["stars"])]

    msg = await bot.send_invoice(
        chat_id=uid,
        title="Пополнение токенов",
        description=f"{pkg['stars']} ⭐ → {pkg['tokens']} токенов",
        payload=payload,
        provider_token="",           # Stars
        currency="XTR",              # Stars
        prices=prices,
        start_parameter=f"stars_{pack}_{uid}",
        is_flexible=False,
    )
    LAST_INVOICE_MSG[uid] = msg.message_id

# Payments: pre-checkout + successful_payment (Stars)
@dp.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery):
    try:
        await bot.answer_pre_checkout_query(pcq.id, ok=True)
    except Exception:
        logging.exception("pre_checkout answer error")

@dp.message(F.successful_payment)
async def on_successful_stars_payment(message: Message):
    sp = message.successful_payment
    if not sp or sp.currency != "XTR":
        return

    try:
        payload = json.loads(sp.invoice_payload or "{}")
    except Exception:
        payload = {}

    uid = message.from_user.id
    stars_paid = int(sp.total_amount)
    charge_id = sp.telegram_payment_charge_id
    tokens = int(payload.get("tokens") or 0)
    pack_stars_declared = int(payload.get("stars") or 0)

    if pack_stars_declared and pack_stars_declared != stars_paid:
        logging.warning(
            f"Stars mismatch: declared={pack_stars_declared}, paid={stars_paid}, payload={payload}"
        )

    applied = False
    try:
        if hasattr(db, "apply_star_payment"):
            applied = await db.apply_star_payment(
                user_id=uid,
                telegram_payment_charge_id=charge_id,
                stars=stars_paid,
                tokens=tokens,
                raw_payload=payload,
            )
        else:
            if charge_id in APPLIED_CHARGES:
                applied = False
            else:
                await db.add_generations(uid, tokens)
                APPLIED_CHARGES.add(charge_id)
                applied = True
    except Exception:
        logging.exception("apply_star_payment error")
        try:
            await db.add_generations(uid, tokens)
            applied = True
        except Exception:
            logging.exception("add_generations fallback error")

    if applied:
        await message.answer(
            f"✅ Оплата получена: {stars_paid} ⭐\n"
            f"🪙 Начислено: {tokens} токенов\nСпасибо! 🎉"
        )
    else:
        await message.answer("ℹ️ Этот платёж уже был учтён ранее.")

    # Удаляем чек (текущее сообщение) и инвойс Stars
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        pass
    mid = LAST_INVOICE_MSG.pop(uid, None)
    if mid:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=mid)
        except Exception:
            pass

# ───── YooKassa: Рубли без вебхуков (поллинг статуса) ─────
RUB_PACKS = {
    "30":  {"rubles": 30,  "tokens": 30},
    "100": {"rubles": 100, "tokens": 100},
    "200": {"rubles": 200, "tokens": 200},
    "500": {"rubles": 500, "tokens": 500},
}

def create_yookassa_payment(amount_rub: int, user_id: int, tokens: int):
    payment = Payment.create({
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
        "capture": True,
        "description": f"Пополнение {amount_rub}₽ ({tokens} токенов) пользователем {user_id}",
        "metadata": {"user_id": user_id, "tokens": tokens},
        "receipt": {
            "customer": {
                "email": "antipingv2003@gmail.com"  # или телефон: "phone": "+79998887766"
            },
            "items": [{
                "description": f"{tokens} токенов",
                "quantity": "1.0",
                "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
                "vat_code": "1"  # 1 — без НДС
            }]
        }
    })
    return payment.confirmation.confirmation_url, payment.id


async def check_yookassa_payment(payment_id: str, user_id: int, tokens: int):
    """
    Ожидаем платёж (опрос раз в 10с, максимум 5 минут).
    Начисляем токены при статусе succeeded.
    """
    try:
        for _ in range(30):
            payment = await asyncio.to_thread(Payment.find_one, payment_id)
            status = getattr(payment, "status", None)
            if status == "succeeded":
                await db.add_generations(user_id, tokens)
                await bot.send_message(user_id, f"✅ Оплата {payment.amount.value}₽ получена.\n🪙 Начислено {tokens} токенов.")
                return True
            if status in ("canceled", "expired"):
                await bot.send_message(user_id, "❌ Оплата не завершена или отменена.")
                return False
            await asyncio.sleep(10)
        await bot.send_message(user_id, "⌛ Время ожидания оплаты истекло. Если оплатили — напишите в поддержку.")
        return False
    except Exception:
        logging.exception("Ошибка при проверке статуса YooKassa")
        await bot.send_message(user_id, "❌ Ошибка при проверке оплаты. Если списало — свяжитесь с поддержкой.")
        return False

@dp.callback_query(F.data == "pay_rub")
async def pay_rub_cb(callback: CallbackQuery, state: FSMContext):
    # Показ пакетов Рубли→Токены
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💵 {RUB_PACKS['30']['rubles']}₽ → {RUB_PACKS['30']['tokens']} токенов",   callback_data="rubles_30")],
        [InlineKeyboardButton(text=f"💵 {RUB_PACKS['100']['rubles']}₽ → {RUB_PACKS['100']['tokens']} токенов", callback_data="rubles_100")],
        [InlineKeyboardButton(text=f"💵 {RUB_PACKS['200']['rubles']}₽ → {RUB_PACKS['200']['tokens']} токенов", callback_data="rubles_200")],
        [InlineKeyboardButton(text=f"💵 {RUB_PACKS['500']['rubles']}₽ → {RUB_PACKS['500']['tokens']} токенов", callback_data="rubles_500")],
        [back_btn("top_up_balance")]
    ])
    await callback.message.edit_text("💵 Выберите пакет для пополнения (YooKassa):", reply_markup=kb)

@dp.callback_query(F.data.startswith("rubles_"))
async def rubles_package_cb(callback: CallbackQuery):
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        await callback.answer("YooKassa не настроена", show_alert=True)
        return

    uid = callback.from_user.id
    pack = callback.data.split("_")[1]  # "30" | "100" | "200" | "500"

    if pack not in RUB_PACKS:
        await callback.answer("❌ Неверный пакет", show_alert=True)
        return
    pkg = RUB_PACKS[pack]

    try:
        # создаём платёж (SDK синхронный → оборачиваем в to_thread)
        pay_url, pay_id = await asyncio.to_thread(create_yookassa_payment, pkg["rubles"], uid, pkg["tokens"])
        await callback.message.edit_text(
            f"💳 Счёт на {pkg['rubles']}₽ создан.\n"
            "Перейдите по кнопке ниже, чтобы оплатить.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💰 Оплатить в YooKassa", url=pay_url)],
                    [back_btn("pay_rub")]
                ]
            )
        )
        # запускаем проверку статуса
        asyncio.create_task(check_yookassa_payment(pay_id, uid, pkg["tokens"]))

    except Exception:
        logging.exception("Ошибка при создании платежа YooKassa")
        await callback.message.edit_text("❌ Не удалось создать платёж. Попробуйте позже.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn("pay_rub")]]))

# ───────────────────────── Интеграция с KIE ───────────────────────────
def _input_payload(prompt: str, duration: int, orientation: str,
                   image_url: str | None, tier: str, quality: str | None):
    """
    Формируем тело input:
    - n_frames, remove_watermark, prompt
    - aspect_ratio (всегда)
    - size ('standard' | 'high') для Sora 2 Pro
    - image_urls при I2V
    """
    p: dict = {
        "prompt": prompt,
        "n_frames": _map_n_frames(duration),
        "remove_watermark": True,
        "aspect_ratio": _map_aspect_ratio(orientation)
    }
    if image_url:
        p["image_urls"] = [image_url]
    if tier == "sora2_pro":
        p["size"] = "high" if quality == "high" else "standard"
    return p

async def send_to_kie_api(uid: int, model: str, prompt: str, duration: int,
                          orientation: str, image_url: str | None,
                          cost: int, tier: str, quality: str | None, ptype: str):
    payload = {"model": model,
               "input": _input_payload(prompt, duration, orientation, image_url, tier, quality)}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(JOBS_CREATE, json=payload, headers=_kie_headers(), timeout=120) as r:
                data = await r.json(content_type=None)
                if r.status != 200 or data.get("code") != 200:
                    await db.add_generations(uid, cost)
                    raise RuntimeError(f"KIE createTask error: status={r.status}, body={data}")
                task_id = (data.get("data") or {}).get("taskId") or (data.get("data") or {}).get("task_id")
                if not task_id:
                    await db.add_generations(uid, cost)
                    raise RuntimeError(f"KIE createTask: нет taskId в ответе: {data}")
    except Exception:
        logging.exception("Ошибка при отправке в KIE")
        await db.add_generations(uid, cost)
        await bot.send_message(uid, "❌ Не удалось создать задачу. Токены возвращены.")
        raise

    asyncio.create_task(check_video_status(uid, task_id, duration, orientation, cost))

async def check_video_status(uid: int, task_id: str, duration: int, orientation: str, cost: int):
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(90):  # ~12 минут, шаг 8с
                async with s.get(
                    JOBS_STATUS,
                    params={"taskId": task_id},
                    headers=_kie_headers(),
                    timeout=30
                ) as r:
                    result = await r.json(content_type=None)
                    if r.status != 200 or result.get("code") != 200:
                        await asyncio.sleep(8); continue

                    d = result.get("data") or {}
                    state = (d.get("state") or "").lower()
                    flag = d.get("successFlag")

                    if state in ("", "wait", "queueing", "generating") or flag == 0:
                        await asyncio.sleep(8); continue

                    if state == "success" or flag == 1:
                        video_url = None
                        resp_obj = d.get("response") or {}
                        video_url = resp_obj.get("videoUrl")
                        urls = resp_obj.get("resultUrls")
                        if not video_url and isinstance(urls, list) and urls:
                            video_url = urls[0]

                        if not video_url and d.get("resultJson"):
                            try:
                                rj = d["resultJson"]
                                rj = json.loads(rj) if isinstance(rj, str) else rj
                                video_url = rj.get("result")
                                if not video_url:
                                    r_urls = rj.get("resultUrls")
                                    if isinstance(r_urls, list) and r_urls:
                                        video_url = r_urls[0]
                            except Exception:
                                pass

                        line_orient = f", 📱 {orientation}" if orientation else ""
                        await bot.send_message(uid, f"🎉 Ваше видео готово! ⏱️ {duration} с{line_orient}")
                        if video_url:
                            await bot.send_video(chat_id=uid, video=video_url, caption="🎬 Готовый ролик")
                        else:
                            await bot.send_message(uid, "⚠️ Видео готово, но URL не найден в ответе.")
                        return

                    # ошибка
                    fail_msg = d.get("failMsg") or d.get("errorMessage") or "Ошибка генерации"
                    await db.add_generations(uid, cost)
                    await bot.send_message(uid, f"❌ Генерация не удалась: {fail_msg}. Токены возвращены.")
                    return

                await asyncio.sleep(8)

            # таймаут
            await db.add_generations(uid, cost)
            await bot.send_message(uid, "⏳ Истекло время ожидания. Токены возвращены.")
    except Exception:
        logging.exception("Ошибка при проверке статуса видео")
        await db.add_generations(uid, cost)
        await bot.send_message(uid, "❌ Ошибка при генерации. Токены возвращены.")

# ───────────────────────────── Точка входа ────────────────────────────
async def main():
    try:
        await db.connect()
        logging.info("DB connected")
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Ошибка при запуске бота: {e}")
    finally:
        await db.close()
        logging.info("DB closed")

if __name__ == "__main__":
    asyncio.run(main())
