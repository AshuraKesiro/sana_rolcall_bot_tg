"""
Telegram-бот для учёта посещаемости студентов в хостеле.
Роли: Ментор | ТЖОшник (админ)
Установка: pip install aiogram aiosqlite apscheduler
"""

import asyncio
import json
import logging
from datetime import date
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ══════════════════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN = "7934725290:AAFcQz9JkrLOall-AAvpt4pEuA_n9IoHZXY"

MENTORS: dict[int, str] = {
    5012943269: "Данияр",
    1105194652: "Бибарыс",
    472432309:  "Алмас sr",
    877549942:  "Алмас jr",
    87066412960: "Еразамат",
    655465561: "Мұхитәли",
    1231111202: "Дәулет"
}

ADMINS: dict[int, str] = {
    1119685866: "Администратор",
}

REMINDER_HOUR   = 20
REMINDER_MINUTE = 0
DB_PATH = "hostel.db"

# ── производные множества ─────────────────────────────────────────────────────
MENTOR_IDS = set(MENTORS.keys())
ADMIN_IDS  = set(ADMINS.keys())
ALL_IDS    = MENTOR_IDS | ADMIN_IDS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════════════════════════════════════

class MS(StatesGroup):
    # Управление списком
    adding_students   = State()
    deleting_students = State()
    # Посещаемость — 4 шага + ввод причины
    att_present     = State()   # шаг 1
    att_receipt     = State()   # шаг 2: список
    att_receipt_why = State()   # шаг 2: ввод причины
    att_warned      = State()   # шаг 3: список
    att_warned_why  = State()   # шаг 3: ввод причины
    att_no_reason   = State()   # шаг 4

# ══════════════════════════════════════════════════════════════════════════════
#  БД
# ══════════════════════════════════════════════════════════════════════════════

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS mentors (
                tg_id INTEGER PRIMARY KEY,
                name  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS students (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                mentor_id INTEGER NOT NULL,
                name      TEXT NOT NULL,
                FOREIGN KEY (mentor_id) REFERENCES mentors(tg_id)
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                mentor_id   INTEGER NOT NULL,
                report_date TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (mentor_id) REFERENCES mentors(tg_id)
            );
        """)
        await db.commit()
        for mid, mname in {**MENTORS, **ADMINS}.items():
            await db.execute(
                "INSERT OR IGNORE INTO mentors (tg_id, name) VALUES (?, ?)",
                (mid, mname)
            )
        await db.commit()


def get_name(uid: int) -> str:
    return MENTORS.get(uid) or ADMINS.get(uid) or f"User_{uid}"


async def get_students(mentor_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM students WHERE mentor_id = ? ORDER BY name",
            (mentor_id,)
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def add_students(mentor_id: int, names: list[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        existing = set(await get_students(mentor_id))
        for name in names:
            n = name.strip()
            if n and n not in existing:
                await db.execute(
                    "INSERT INTO students (mentor_id, name) VALUES (?, ?)",
                    (mentor_id, n)
                )
        await db.commit()


async def delete_student(mentor_id: int, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM students WHERE mentor_id = ? AND name = ?",
            (mentor_id, name)
        )
        await db.commit()


async def save_report(mentor_id: int, report: dict):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM attendance WHERE mentor_id = ? AND report_date = ?",
            (mentor_id, today)
        )
        await db.execute(
            "INSERT INTO attendance (mentor_id, report_date, report_json) VALUES (?, ?, ?)",
            (mentor_id, today, json.dumps(report, ensure_ascii=False))
        )
        await db.commit()


async def get_reports_for_date(day: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT mentor_id, report_json FROM attendance "
            "WHERE report_date = ? ORDER BY created_at", (day,)
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        d = json.loads(r[1])
        d["mentor_id"] = r[0]
        result.append(d)
    return result


async def get_report_for_mentor_date(mentor_id: int, day: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT report_json FROM attendance WHERE mentor_id = ? AND report_date = ?",
            (mentor_id, day)
        ) as cur:
            row = await cur.fetchone()
    if row:
        d = json.loads(row[0])
        d["mentor_id"] = mentor_id
        return d
    return None


async def get_all_report_dates() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT report_date FROM attendance ORDER BY report_date DESC"
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _remaining(data: dict) -> list[str]:
    """Студенты, ещё не попавшие ни в одну категорию (кроме шага 4)."""
    done = (set(data.get("present", [])) |
            set(data.get("with_receipt", {}).keys()) |
            set(data.get("warned", {}).keys()))
    return [s for s in data.get("students", []) if s not in done]


def fmt_report(report: dict) -> str:
    mname = report.get("mentor_name", "?")
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 *Ментор: {mname}*",
        "━━━━━━━━━━━━━━━━━━━━━━", "",
        "✅ *Присутствуют:*",
    ]
    present = report.get("present", [])
    lines += [f"  ▸ {n}" for n in present] if present else ["  _— нет —_"]

    lines += ["", "📋 *Отсутствуют (с распиской):*"]
    wr = report.get("with_receipt", {})
    lines += [f"  ▸ {n}\n    📌 _{r}_" for n, r in wr.items()] if wr else ["  _— нет —_"]

    lines += ["", "⚠️ *Отсутствуют (без расписки, предупредили):*"]
    warned = report.get("warned", {})
    lines += [f"  ▸ {n}\n    📌 _{r}_" for n, r in warned.items()] if warned else ["  _— нет —_"]

    lines += ["", "❌ *Отсутствуют (без причины):*"]
    nr = report.get("no_reason", [])
    lines += [f"  ▸ {n}" for n in nr] if nr else ["  _— нет —_"]

    lines.append("")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════════════════════

def main_kb(uid: int) -> InlineKeyboardMarkup:
    if uid in MENTOR_IDS:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Отметить посещаемость", callback_data="m_attendance")],
            [InlineKeyboardButton(text="✏️ Управление списком",    callback_data="m_manage_list")],
            [InlineKeyboardButton(text="👀 Посмотреть записи",     callback_data="v_start")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👀 Посмотреть записи", callback_data="v_start")],
    ])


def manage_list_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить студентов", callback_data="ml_add")],
        [InlineKeyboardButton(text="➖ Удалить студентов",  callback_data="ml_del")],
        [InlineKeyboardButton(text="🔙 Назад",              callback_data="go_main")],
    ])


def back_to_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="go_main")
    ]])


def delete_select_kb(students: list[str], selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for name in students:
        mark = "🗑 " if name in selected else "◻️ "
        rows.append([InlineKeyboardButton(
            text=f"{mark}{name}", callback_data=f"del_pick:{name}"
        )])
    rows.append([
        InlineKeyboardButton(text="🔙 Назад",             callback_data="m_manage_list"),
        InlineKeyboardButton(text="✅ Удалить выбранных",  callback_data="del_confirm"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Клавиатуры посещаемости ───────────────────────────────────────────────────
# Каждый шаг имеет кнопку «Назад» на предыдущий шаг.
# Прогресс (выборы на предыдущих шагах) НЕ теряется.

def att_step1_kb(students: list[str], selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for name in students:
        mark = "✅ " if name in selected else "◻️ "
        rows.append([InlineKeyboardButton(
            text=f"{mark}{name}", callback_data=f"pres:{name}"
        )])
    rows.append([
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="att_to_main"),
        InlineKeyboardButton(text="➡️ Далее",         callback_data="pres_next"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def att_step2_kb(remaining: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"wr:{name}")]
            for name in remaining]
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back_s1"),
        InlineKeyboardButton(text="➡️ Далее", callback_data="wr_next"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def att_step2_why_kb() -> InlineKeyboardMarkup:
    """Клавиатура при вводе причины на шаге 2 — Назад возвращает к списку шага 2."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад (без сохранения)", callback_data="back_s2_list"),
    ]])


def att_step3_kb(remaining: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"wa:{name}")]
            for name in remaining]
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back_s2"),
        InlineKeyboardButton(text="➡️ Далее", callback_data="wa_next"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def att_step3_why_kb() -> InlineKeyboardMarkup:
    """Назад при вводе причины шага 3."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад (без сохранения)", callback_data="back_s3_list"),
    ]])


def att_step4_kb(remaining: list[str], selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for name in remaining:
        mark = "✅ " if name in selected else "◻️ "
        rows.append([InlineKeyboardButton(
            text=f"{mark}{name}", callback_data=f"nr:{name}"
        )])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад",    callback_data="back_s3"),
        InlineKeyboardButton(text="✅ Завершить", callback_data="nr_finish"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def att_step4_empty_kb() -> InlineKeyboardMarkup:
    """Шаг 4 без оставшихся студентов."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад",    callback_data="back_s3"),
        InlineKeyboardButton(text="✅ Завершить", callback_data="nr_finish"),
    ]])


# ── Просмотр ──────────────────────────────────────────────────────────────────

def period_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня",       callback_data="v_today")],
        [InlineKeyboardButton(text="📆 Прошедшие дни", callback_data="v_past")],
        [InlineKeyboardButton(text="🔙 Назад",          callback_data="go_main")],
    ])


def mentor_pick_kb(day: str, back_cb: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="👥 Все менторы", callback_data=f"v_all:{day}")]]
    for mid, mname in MENTORS.items():
        rows.append([InlineKeyboardButton(
            text=f"👤 {mname}", callback_data=f"v_one:{day}:{mid}"
        )])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_kb(dates: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📆 {d}", callback_data=f"v_date:{d}")]
            for d in dates]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="v_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ══════════════════════════════════════════════════════════════════════════════
#  РОУТЕР
# ══════════════════════════════════════════════════════════════════════════════

router = Router()

# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    if uid not in ALL_IDS:
        return await msg.answer("⛔ У вас нет доступа к этому боту.")
    await msg.answer(
        f"👋 Привет, *{get_name(uid)}*!\nВыберите действие:",
        parse_mode="Markdown", reply_markup=main_kb(uid)
    )


@router.callback_query(F.data == "go_main")
async def cb_go_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    if uid not in ALL_IDS:
        return await call.answer("⛔ Нет доступа")
    await call.message.edit_text(
        "🏠 *Главное меню:*", parse_mode="Markdown", reply_markup=main_kb(uid)
    )
    await call.answer()

# ══════════════════════════════════════════════════════════════════════════════
#  УПРАВЛЕНИЕ СПИСКОМ СТУДЕНТОВ
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "m_manage_list")
async def cb_manage_list(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in MENTOR_IDS:
        return await call.answer("⛔ Нет доступа")
    await state.clear()
    students = await get_students(call.from_user.id)
    txt = ("📋 *Текущий список студентов:*\n" + "\n".join(f"  ▸ {n}" for n in students)
           if students else "📋 Список студентов пока пуст.")
    await call.message.edit_text(
        txt + "\n\n_Выберите действие:_",
        parse_mode="Markdown", reply_markup=manage_list_kb()
    )
    await call.answer()


@router.callback_query(F.data == "ml_add")
async def cb_ml_add(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in MENTOR_IDS:
        return await call.answer("⛔ Нет доступа")
    await state.set_state(MS.adding_students)
    await call.message.edit_text(
        "➕ *Добавление студентов*\n\n"
        "Введите имена новых студентов — *каждое с новой строки*:\n\n"
        "_Пример:_\nАлихан Сейтов\nДана Нурова",
        parse_mode="Markdown", reply_markup=back_to_main_kb()
    )
    await call.answer()


@router.message(MS.adding_students)
async def msg_add_students(msg: Message, state: FSMContext):
    names = [n.strip() for n in msg.text.splitlines() if n.strip()]
    if not names:
        return await msg.answer("❗ Пустой список.", reply_markup=back_to_main_kb())
    await add_students(msg.from_user.id, names)
    await state.clear()
    students = await get_students(msg.from_user.id)
    txt = (f"✅ Добавлено: *{len(names)}* студент(ов).\n\n"
           "📋 *Текущий список:*\n" + "\n".join(f"  ▸ {n}" for n in students))
    await msg.answer(txt, parse_mode="Markdown", reply_markup=manage_list_kb())


@router.callback_query(F.data == "ml_del")
async def cb_ml_del(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in MENTOR_IDS:
        return await call.answer("⛔ Нет доступа")
    students = await get_students(call.from_user.id)
    if not students:
        return await call.answer("Список пуст!", show_alert=True)
    await state.set_state(MS.deleting_students)
    await state.update_data(del_selected=[], students=students)
    await call.message.edit_text(
        "🗑 *Удаление студентов*\n\nВыберите кого удалить:",
        parse_mode="Markdown",
        reply_markup=delete_select_kb(students, set())
    )
    await call.answer()


@router.callback_query(MS.deleting_students, F.data.startswith("del_pick:"))
async def cb_del_pick(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    data = await state.get_data()
    sel: list = data.get("del_selected", [])
    sel.remove(name) if name in sel else sel.append(name)
    await state.update_data(del_selected=sel)
    await call.message.edit_reply_markup(
        reply_markup=delete_select_kb(data["students"], set(sel))
    )
    await call.answer()


@router.callback_query(MS.deleting_students, F.data == "del_confirm")
async def cb_del_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel: list = data.get("del_selected", [])
    if not sel:
        return await call.answer("Никто не выбран!", show_alert=True)
    for name in sel:
        await delete_student(call.from_user.id, name)
    await state.clear()
    students = await get_students(call.from_user.id)
    txt = (f"✅ Удалено: *{len(sel)}* студент(ов).\n\n"
           + ("📋 *Текущий список:*\n" + "\n".join(f"  ▸ {n}" for n in students)
              if students else "Список теперь пуст."))
    await call.message.edit_text(txt, parse_mode="Markdown", reply_markup=manage_list_kb())
    await call.answer()

# ══════════════════════════════════════════════════════════════════════════════
#  ПОСЕЩАЕМОСТЬ
#
#  Навигация:
#    Шаг 1 → [Главное меню] [Далее →]
#    Шаг 2 → [← Назад к шагу 1] [Далее →]
#    Шаг 2 (ввод причины) → [← Назад (без сохранения)]
#    Шаг 3 → [← Назад к шагу 2] [Далее →]
#    Шаг 3 (ввод причины) → [← Назад (без сохранения)]
#    Шаг 4 → [← Назад к шагу 3] [✅ Завершить]
#
#  При нажатии «Назад» прогресс предыдущих шагов сохраняется.
#  Единственное что теряется — текущий незавершённый ввод причины.
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "m_attendance")
async def cb_att_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in MENTOR_IDS:
        return await call.answer("⛔ Нет доступа")
    students = await get_students(call.from_user.id)
    if not students:
        await call.message.edit_text(
            "❗ Список студентов пуст. Сначала добавьте студентов.",
            reply_markup=manage_list_kb()
        )
        return await call.answer()
    await state.update_data(
        students=students, present=[],
        with_receipt={}, warned={}, no_reason=[], cur_student=None,
    )
    await state.set_state(MS.att_present)
    await call.message.edit_text(
        "📝 *Шаг 1 / 4 — Присутствующие*\n\n"
        "Отметьте всех, кто *присутствует* сегодня:",
        parse_mode="Markdown",
        reply_markup=att_step1_kb(students, set())
    )
    await call.answer()


# ─── Шаг 1: присутствующие ───────────────────────────────────────────────────

@router.callback_query(MS.att_present, F.data.startswith("pres:"))
async def cb_toggle_present(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    data = await state.get_data()
    lst: list = data["present"]
    lst.remove(name) if name in lst else lst.append(name)
    await state.update_data(present=lst)
    await call.message.edit_reply_markup(
        reply_markup=att_step1_kb(data["students"], set(lst))
    )
    await call.answer()


@router.callback_query(MS.att_present, F.data == "pres_next")
async def cb_pres_next(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _show_step2(call, state)


# «Главное меню» с шага 1 — прогресс сбрасывается (шаг 1 самый первый, терять нечего)
@router.callback_query(MS.att_present, F.data == "att_to_main")
async def cb_att_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "🏠 *Главное меню:*", parse_mode="Markdown", reply_markup=main_kb(call.from_user.id)
    )
    await call.answer()


# ─── Шаг 2: с распиской ──────────────────────────────────────────────────────

async def _show_step2(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    remaining = _remaining(data)
    await state.set_state(MS.att_receipt)
    if not remaining:
        return await _show_step3(call, state)
    await call.message.edit_text(
        "📋 *Шаг 2 / 4 — Отсутствуют с распиской*\n\n"
        "Выберите студента — бот попросит причину.\n"
        "Когда закончите — нажмите *Далее*.",
        parse_mode="Markdown",
        reply_markup=att_step2_kb(remaining)
    )


@router.callback_query(MS.att_receipt, F.data.startswith("wr:"))
async def cb_pick_receipt(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(cur_student=name)
    await state.set_state(MS.att_receipt_why)
    await call.message.edit_text(
        f"📝 Введите причину для *{name}* (с распиской):",
        parse_mode="Markdown",
        reply_markup=att_step2_why_kb()
    )
    await call.answer()


@router.message(MS.att_receipt_why)
async def msg_receipt_why(msg: Message, state: FSMContext):
    data = await state.get_data()
    name = data["cur_student"]
    wr: dict = data["with_receipt"]
    wr[name] = msg.text.strip()
    await state.update_data(with_receipt=wr, cur_student=None)
    await state.set_state(MS.att_receipt)
    remaining = _remaining(await state.get_data())
    await msg.answer(
        f"✅ *{name}* — записан с распиской.\n\n"
        + ("Выберите следующего или нажмите «Далее»:"
           if remaining else "Все обработаны. Нажмите «Далее»."),
        parse_mode="Markdown",
        reply_markup=att_step2_kb(remaining)
    )


# «Назад» при вводе причины шага 2 — возвращает к списку шага 2, причина не сохраняется
@router.callback_query(MS.att_receipt_why, F.data == "back_s2_list")
async def cb_back_s2_list(call: CallbackQuery, state: FSMContext):
    await state.update_data(cur_student=None)
    await state.set_state(MS.att_receipt)
    data = await state.get_data()
    remaining = _remaining(data)
    await call.message.edit_text(
        "📋 *Шаг 2 / 4 — Отсутствуют с распиской*\n\n"
        "Выберите студента или нажмите «Далее»:",
        parse_mode="Markdown",
        reply_markup=att_step2_kb(remaining)
    )
    await call.answer()


# «Назад» с шага 2 → возврат на шаг 1 (записи шага 2 не теряются!)
@router.callback_query(MS.att_receipt, F.data == "back_s1")
async def cb_back_s1(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(MS.att_present)
    await call.message.edit_text(
        "📝 *Шаг 1 / 4 — Присутствующие*\n\n"
        "Ваш выбор сохранён. Можете изменить:",
        parse_mode="Markdown",
        reply_markup=att_step1_kb(data["students"], set(data.get("present", [])))
    )
    await call.answer()


@router.callback_query(MS.att_receipt, F.data == "wr_next")
async def cb_wr_next(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _show_step3(call, state)


# ─── Шаг 3: без расписки, предупредили ──────────────────────────────────────

async def _show_step3(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    remaining = _remaining(data)
    await state.set_state(MS.att_warned)
    if not remaining:
        return await _show_step4(call, state)
    await call.message.edit_text(
        "⚠️ *Шаг 3 / 4 — Без расписки, но предупредили*\n\n"
        "Выберите студента — бот попросит причину.\n"
        "Когда закончите — нажмите *Далее*.",
        parse_mode="Markdown",
        reply_markup=att_step3_kb(remaining)
    )


@router.callback_query(MS.att_warned, F.data.startswith("wa:"))
async def cb_pick_warned(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(cur_student=name)
    await state.set_state(MS.att_warned_why)
    await call.message.edit_text(
        f"📝 Введите причину для *{name}* (без расписки, предупредил):",
        parse_mode="Markdown",
        reply_markup=att_step3_why_kb()
    )
    await call.answer()


@router.message(MS.att_warned_why)
async def msg_warned_why(msg: Message, state: FSMContext):
    data = await state.get_data()
    name = data["cur_student"]
    wa: dict = data["warned"]
    wa[name] = msg.text.strip()
    await state.update_data(warned=wa, cur_student=None)
    await state.set_state(MS.att_warned)
    remaining = _remaining(await state.get_data())
    await msg.answer(
        f"✅ *{name}* — записан (предупредил).\n\n"
        + ("Выберите следующего или нажмите «Далее»:"
           if remaining else "Все обработаны. Нажмите «Далее»."),
        parse_mode="Markdown",
        reply_markup=att_step3_kb(remaining)
    )


# «Назад» при вводе причины шага 3 → к списку шага 3
@router.callback_query(MS.att_warned_why, F.data == "back_s3_list")
async def cb_back_s3_list(call: CallbackQuery, state: FSMContext):
    await state.update_data(cur_student=None)
    await state.set_state(MS.att_warned)
    data = await state.get_data()
    remaining = _remaining(data)
    await call.message.edit_text(
        "⚠️ *Шаг 3 / 4 — Без расписки, но предупредили*\n\n"
        "Выберите студента или нажмите «Далее»:",
        parse_mode="Markdown",
        reply_markup=att_step3_kb(remaining)
    )
    await call.answer()


# «Назад» с шага 3 → возврат на шаг 2 (записи шага 3 не теряются!)
@router.callback_query(MS.att_warned, F.data == "back_s2")
async def cb_back_s2(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    remaining_s2 = _remaining_for_s2(data)
    await state.set_state(MS.att_receipt)
    await call.message.edit_text(
        "📋 *Шаг 2 / 4 — Отсутствуют с распиской*\n\n"
        "Ваш выбор сохранён. Можете добавить ещё или нажать «Далее»:",
        parse_mode="Markdown",
        reply_markup=att_step2_kb(remaining_s2)
    )
    await call.answer()


def _remaining_for_s2(data: dict) -> list[str]:
    """Для возврата на шаг 2: показываем студентов, не попавших в «присутствуют»."""
    done = set(data.get("present", []))
    return [s for s in data.get("students", []) if s not in done]


@router.callback_query(MS.att_warned, F.data == "wa_next")
async def cb_wa_next(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _show_step4(call, state)


# ─── Шаг 4: без причины ──────────────────────────────────────────────────────

async def _show_step4(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    remaining = _remaining(data)
    await state.set_state(MS.att_no_reason)
    if not remaining:
        await call.message.edit_text(
            "❌ *Шаг 4 / 4 — Без причины*\n\n"
            "_Все студенты уже распределены по категориям._\n"
            "Нажмите «Завершить» для сохранения отчёта.",
            parse_mode="Markdown",
            reply_markup=att_step4_empty_kb()
        )
    else:
        await call.message.edit_text(
            "❌ *Шаг 4 / 4 — Без причины*\n\n"
            "Отметьте кто отсутствует *без причины*.\n"
            "Если таких нет — просто нажмите «Завершить».",
            parse_mode="Markdown",
            reply_markup=att_step4_kb(remaining, set(data.get("no_reason", [])))
        )


@router.callback_query(MS.att_no_reason, F.data.startswith("nr:"))
async def cb_toggle_no_reason(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    data = await state.get_data()
    remaining = _remaining(data)
    nr: list = data["no_reason"]
    nr.remove(name) if name in nr else nr.append(name)
    await state.update_data(no_reason=nr)
    await call.message.edit_reply_markup(
        reply_markup=att_step4_kb(remaining, set(nr))
    )
    await call.answer()


# «Назад» с шага 4 → возврат на шаг 3 (no_reason не теряется!)
@router.callback_query(MS.att_no_reason, F.data == "back_s3")
async def cb_back_s3(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    remaining_s3 = _remaining_for_s3(data)
    await state.set_state(MS.att_warned)
    await call.message.edit_text(
        "⚠️ *Шаг 3 / 4 — Без расписки, но предупредили*\n\n"
        "Ваш выбор сохранён. Можете добавить ещё или нажать «Далее»:",
        parse_mode="Markdown",
        reply_markup=att_step3_kb(remaining_s3)
    )
    await call.answer()


def _remaining_for_s3(data: dict) -> list[str]:
    """Для возврата на шаг 3: студенты без «присутствуют» и без «с распиской»."""
    done = set(data.get("present", [])) | set(data.get("with_receipt", {}).keys())
    return [s for s in data.get("students", []) if s not in done]


@router.callback_query(MS.att_no_reason, F.data == "nr_finish")
async def cb_nr_finish(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _do_finish(call, state)


async def _do_finish(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mid = call.from_user.id
    report = {
        "date":         date.today().isoformat(),
        "mentor_name":  get_name(mid),
        "present":      data.get("present", []),
        "with_receipt": data.get("with_receipt", {}),
        "warned":       data.get("warned", {}),
        "no_reason":    data.get("no_reason", []),
    }
    await save_report(mid, report)
    await state.clear()
    text = "✅ *Отчёт сохранён!*\n\n" + fmt_report(report)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=main_kb(mid))

# ══════════════════════════════════════════════════════════════════════════════
#  ПРОСМОТР ЗАПИСЕЙ
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "v_start")
async def cb_v_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ALL_IDS:
        return await call.answer("⛔ Нет доступа")
    await state.clear()
    await call.message.edit_text(
        "👀 *Просмотр записей*\n\nВыберите период:",
        parse_mode="Markdown", reply_markup=period_kb()
    )
    await call.answer()


@router.callback_query(F.data == "v_today")
async def cb_v_today(call: CallbackQuery):
    today = date.today().isoformat()
    await call.message.edit_text(
        f"📅 *Сегодня ({today})*\n\nВыберите ментора:",
        parse_mode="Markdown",
        reply_markup=mentor_pick_kb(today, back_cb="v_start")
    )
    await call.answer()


@router.callback_query(F.data == "v_past")
async def cb_v_past(call: CallbackQuery):
    all_dates = await get_all_report_dates()
    today = date.today().isoformat()
    past = [d for d in all_dates if d != today]
    if not past:
        return await call.answer("Нет данных за прошедшие дни.", show_alert=True)
    await call.message.edit_text(
        "📆 *Прошедшие дни*\n\nВыберите дату:",
        parse_mode="Markdown", reply_markup=dates_kb(past)
    )
    await call.answer()


@router.callback_query(F.data.startswith("v_date:"))
async def cb_v_date(call: CallbackQuery):
    day = call.data.split(":", 1)[1]
    await call.message.edit_text(
        f"📆 *{day}*\n\nВыберите ментора:",
        parse_mode="Markdown",
        reply_markup=mentor_pick_kb(day, back_cb="v_past")
    )
    await call.answer()


@router.callback_query(F.data.startswith("v_all:"))
async def cb_v_all(call: CallbackQuery):
    day = call.data.split(":", 1)[1]
    reports = await get_reports_for_date(day)
    back_cb = "v_today" if day == date.today().isoformat() else f"v_date:{day}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb)
    ]])
    if not reports:
        await call.message.edit_text(
            f"📋 За *{day}* отчётов пока нет.",
            parse_mode="Markdown", reply_markup=kb
        )
        return await call.answer()
    parts = [f"📋 *Все отчёты за {day}*\n"]
    for r in reports:
        parts.append(fmt_report(r))
    await call.message.edit_text("\n".join(parts)[:4090], parse_mode="Markdown", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("v_one:"))
async def cb_v_one(call: CallbackQuery):
    _, day, mid_str = call.data.split(":", 2)
    mid = int(mid_str)
    report = await get_report_for_mentor_date(mid, day)
    mname = get_name(mid)
    back_cb = "v_today" if day == date.today().isoformat() else f"v_date:{day}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb)
    ]])
    if not report:
        await call.message.edit_text(
            f"📋 *{mname}* — за *{day}* отчёт не сдан.",
            parse_mode="Markdown", reply_markup=kb
        )
        return await call.answer()
    await call.message.edit_text(
        fmt_report(report)[:4090], parse_mode="Markdown", reply_markup=kb
    )
    await call.answer()

# ══════════════════════════════════════════════════════════════════════════════
#  НАПОМИНАНИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def send_reminders(bot: Bot):
    today = date.today().isoformat()
    reports = await get_reports_for_date(today)
    done_ids = {r["mentor_id"] for r in reports}
    for mid in MENTOR_IDS:
        if mid not in done_ids:
            try:
                await bot.send_message(
                    mid,
                    "🔔 *Напоминание!*\nНе забудьте отметить посещаемость за сегодня.",
                    parse_mode="Markdown", reply_markup=main_kb(mid)
                )
            except Exception as e:
                log.warning(f"Напоминание не отправлено {mid}: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_reminders, "cron",
        hour=REMINDER_HOUR, minute=REMINDER_MINUTE,
        args=[bot]
    )
    scheduler.start()

    log.info("✅ Бот запущен!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
