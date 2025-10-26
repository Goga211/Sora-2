import os
import json
import asyncio
import logging
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from database import db  # ваш модуль Database с глобальным экземпляром db

# ──────────────────────────── Настройка ───────────────────────────────
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("TOKEN не найден в .env")

KIE_API_BASE = os.getenv("KIE_API_BASE", "https://api.kie.ai")
KIE_API_KEY = os.getenv("KIE_API_KEY")
if not KIE_API_KEY:
    raise ValueError("KIE_API_KEY не найден в .env")

JOBS_CREATE = f"{KIE_API_BASE}/api/v1/jobs/createTask"
JOBS_STATUS = f"{KIE_API_BASE}/api/v1/jobs/recordInfo"

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ──────────────────────────── Состояния ───────────────────────────────
class VideoCreationStates(StatesGroup):
    waiting_for_prompt_type = State()
    waiting_for_model_tier = State()
    waiting_for_quality = State()               # только для Pro
    waiting_for_duration_orientation = State()  # для всех: длительность + ориентация
    waiting_for_image = State()
    waiting_for_prompt = State()
    waiting_for_confirmation = State()

class BalanceStates(StatesGroup):
    waiting_for_payment_method = State()

# ──────────────────────────── Цены ────────────────────────────────────
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

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Создать видео", callback_data="create_video")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data="check_balance")],
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="top_up_balance")]
    ])

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

# ─────────────────────────────── Хэндлеры UI ──────────────────────────────────
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
        "💰 Баланс — «Баланс», пополнение — «Пополнить баланс»."
    )
    await message.answer(text, reply_markup=get_main_keyboard())

# старт
@dp.callback_query(F.data == "create_video")
async def start_create_video(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not await db.has_generations(uid):
        await callback.message.edit_text("❌ У вас нет токенов. Пополните баланс.", reply_markup=get_main_keyboard())
        return
    await state.set_state(VideoCreationStates.waiting_for_prompt_type)
    await state.update_data(prompt_type=None, tier=None, quality=None,
                            duration=None, orientation=None,
                            image_url=None, prompt=None, cost=None, kie_model=None)
    await callback.message.edit_text("Выберите тип промпта:", reply_markup=get_prompt_type_keyboard())

# назад в главное меню
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🏠 Главное меню", reply_markup=get_main_keyboard())
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
    # Далее → длительность + ориентация
    tier, q = data.get("tier"), data.get("quality")
    await state.set_state(VideoCreationStates.waiting_for_duration_orientation)
    await callback.message.edit_text(
        duration_price_text(tier, q),
        reply_markup=get_duration_orientation_keyboard(),
        parse_mode="Markdown"
    )

# назад с длительности/ориентации → качество (или модель, если не Pro)
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

# Далее → просим картинку или текст
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
            f"❌ Недостаточно токенов.\nНужно {cost}, у вас {bal}.",
            reply_markup=get_main_keyboard()
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

# ─────────────── Баланс и пополнение (лоты обновлены) ────────────────
@dp.callback_query(F.data == "check_balance")
async def check_balance_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    user = await db.get_user(uid)
    txt = f"💰 Ваш баланс:\n\n🪙 Токенов: {user['generations_left']}" if user else "❌ Пользователь не найден"
    await callback.message.edit_text(txt, reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "top_up_balance")
async def top_up_balance_cb(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Рубли", callback_data="pay_rub")],
        [InlineKeyboardButton(text="⭐ Звёзды", callback_data="pay_stars")],
        [back_btn("back_to_main")]
    ])
    await callback.message.edit_text("💳 Выберите способ пополнения:", reply_markup=kb)
    await state.set_state(BalanceStates.waiting_for_payment_method)

# Рубли — лоты 30/100/200/500 (1:1 в токены)
@dp.callback_query(F.data == "pay_rub")
async def pay_rub_cb(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 30₽ → 30 токенов",   callback_data="rubles_30")],
        [InlineKeyboardButton(text="💵 100₽ → 100 токенов", callback_data="rubles_100")],
        [InlineKeyboardButton(text="💵 200₽ → 200 токенов", callback_data="rubles_200")],
        [InlineKeyboardButton(text="💵 500₽ → 500 токенов", callback_data="rubles_500")],
        [back_btn("top_up_balance")]
    ])
    await callback.message.edit_text("💵 Выберите пакет для пополнения:", reply_markup=kb)

# Звёзды — лоты 20/60/120/300 → 30/100/200/500 токенов
@dp.callback_query(F.data == "pay_stars")
async def pay_stars_cb(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ 20 звёзд → 30 токенов",   callback_data="stars_20")],
        [InlineKeyboardButton(text="⭐ 60 звёзд → 100 токенов",  callback_data="stars_60")],
        [InlineKeyboardButton(text="⭐ 120 звёзд → 200 токенов", callback_data="stars_120")],
        [InlineKeyboardButton(text="⭐ 300 звёзд → 500 токенов", callback_data="stars_300")],
        [back_btn("top_up_balance")]
    ])
    await callback.message.edit_text("⭐ Выберите пакет для пополнения:", reply_markup=kb)

@dp.callback_query(F.data.startswith("stars_"))
async def stars_package_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    pack = callback.data.split("_")[1]  # "20" | "60" | "120" | "300"

    star_packs = {
        "20":  {"stars": 20,  "tokens": 30},
        "60":  {"stars": 60,  "tokens": 100},
        "120": {"stars": 120, "tokens": 200},
        "300": {"stars": 300, "tokens": 500},
    }

    if pack not in star_packs:
        await callback.answer("❌ Неверный пакет", show_alert=True)
        return

    pkg = star_packs[pack]

    # TODO: интеграция с Telegram Stars billing
    await db.add_generations(uid, pkg["tokens"])

    await callback.message.edit_text(
        "✅ Пополнение успешно!\n\n"
        f"⭐ Списано: {pkg['stars']} звёзд\n"
        f"🪙 Начислено: {pkg['tokens']} токенов\n\n"
        "Спасибо! 🎉",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data.startswith("rubles_"))
async def rubles_package_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    pack = callback.data.split("_")[1]  # "30" | "100" | "200" | "500"

    rub_packs = {
        "30":  {"rubles": 30,  "tokens": 30},
        "100": {"rubles": 100, "tokens": 100},
        "200": {"rubles": 200, "tokens": 200},
        "500": {"rubles": 500, "tokens": 500},
    }

    if pack not in rub_packs:
        await callback.answer("❌ Неверный пакет", show_alert=True)
        return

    pkg = rub_packs[pack]

    # TODO: интеграция с платёжной системой
    await db.add_generations(uid, pkg["tokens"])

    await callback.message.edit_text(
        "✅ Пополнение успешно!\n\n"
        f"💵 Оплачено: {pkg['rubles']}₽\n"
        f"🪙 Начислено: {pkg['tokens']} токенов\n\n"
        "Спасибо! 🎉",
        reply_markup=get_main_keyboard()
    )

# ───────────────────────── Интеграция с KIE ────────────────────────────
def _input_payload(prompt: str, duration: int, orientation: str,
                   image_url: str | None, tier: str, quality: str | None):
    """
    Формируем тело input:
    - n_frames, remove_watermark, prompt
    - aspect_ratio (всегда, для обеих линеек)
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

# ───────────────────────────── Точка входа ────────────────────────────────────
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
