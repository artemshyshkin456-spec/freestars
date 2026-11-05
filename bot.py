import asyncio
import logging
import re
import time
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
)

import db
from config import (
    ADMIN_ID,
    BOT_TOKEN,
    BUY_OPTIONS,
    MIN_WITHDRAW,
    PAY_ACCOUNT,
    REF_REWARD,
    SUB_HOLD_DAYS,
    TASK_REWARD,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("starsovearn")

router = Router()
admin_router = Router()
admin_router.message.filter(F.from_user.id == ADMIN_ID, F.chat.type == "private")
admin_router.callback_query.filter(F.from_user.id == ADMIN_ID)

BOT_USERNAME = ""
fmt = db.fmt


# ============================================================ FSM
class Proof(StatesGroup):
    photo = State()


class AddTask(StatesGroup):
    url = State()
    target = State()
    chat = State()


class EditTarget(StatesGroup):
    value = State()


class Bind(StatesGroup):
    post = State()


class AdForm(StatesGroup):
    content = State()
    duration = State()


class BanForm(StatesGroup):
    uid = State()


class Buy(StatesGroup):
    link = State()
    post = State()


class BalForm(StatesGroup):
    uid = State()
    amount = State()


class TikTok(StatesGroup):
    photo = State()


# ============================================================ ХЕЛПЕРЫ
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def tail_int(data: str):
    part = (data or "").rsplit(":", 1)[-1]
    return int(part) if part.isdigit() and len(part) <= 12 else None


def tm(ts) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"


def user_label(uid: int, username, first_name) -> str:
    name = escape(first_name or str(uid))
    uname = f" (@{escape(username)})" if username else ""
    return f'<a href="tg://user?id={uid}">{name}</a>{uname} · <code>{uid}</code>'


def kind_title(kind: str) -> str:
    return "📢 Канал" if kind == "channel" else "🤖 Бот"


async def show(cb: CallbackQuery, text: str, markup=None):
    if not isinstance(cb.message, Message):
        await safe_send(cb.bot, cb.from_user.id, text, reply_markup=markup)
        try:
            await cb.answer()
        except TelegramAPIError:
            pass
        return
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            try:
                await cb.message.answer(text, reply_markup=markup)
            except TelegramAPIError:
                log.exception("show: не удалось отправить сообщение")
    except TelegramAPIError:
        log.exception("show: ошибка Telegram")
    try:
        await cb.answer()
    except TelegramAPIError:
        pass


async def mark_done(cb: CallbackQuery, suffix: str):
    msg = cb.message
    if not isinstance(msg, Message):
        return
    try:
        if msg.photo:
            await msg.edit_caption(caption=f"{msg.caption or ''}\n\n{suffix}", reply_markup=None)
        else:
            await msg.edit_text(f"{msg.html_text}\n\n{suffix}", reply_markup=None)
    except TelegramAPIError:
        pass


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError:
        return None


async def is_member(bot: Bot, chat_id: int, uid: int):
    try:
        m = await bot.get_chat_member(chat_id, uid)
    except TelegramAPIError:
        log.warning("get_chat_member не сработал для чата %s", chat_id)
        return None
    if m.status in ("member", "administrator", "creator"):
        return True
    return m.status == "restricted" and bool(getattr(m, "is_member", False))


def welcome() -> str:
    return (
        "⭐ <b>Добро пожаловать!</b>\n\n"
        f"Подписывайся на каналы и переходи в ботов — за каждое задание "
        f"<b>{fmt(TASK_REWARD)}⭐</b>.\n"
        f"Приглашай друзей — <b>{fmt(REF_REWARD)}⭐</b> за каждого.\n"
        f"Минимальный вывод: <b>{fmt(MIN_WITHDRAW)}⭐</b>."
    )


def main_text(uid: int) -> str:
    info = db.get_user(uid)
    bal = info[0] if info else 0
    return welcome() + f"\n\n💰 <b>Твой баланс: {fmt(bal)}⭐</b>"


def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("💰 Заработать звёзды", "earn")],
        [btn("👤 Профиль", "profile"), btn("👥 Пригласить друзей", "invite")],
        [btn("🎵 Коментарии TikTok", "tiktok")],
        [btn("🛒 Купить подписчиков", "buy")],
        [btn("💸 Вывести звёзды", "withdraw")],
    ]
    if uid == ADMIN_ID:
        rows.append([btn("🛠 Админ-панель", "adm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu() -> InlineKeyboardMarkup:
    return kb([btn("⬅️ Назад", "menu")])


# ============================================================ MIDDLEWARE
class Guard(BaseMiddleware):
    def __init__(self, delay: float = 0.7):
        self.delay = delay
        self.last: dict[int, float] = {}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type != "private":
            return None
        is_start = isinstance(event, Message) and bool(event.text) and event.text.startswith("/start")
        if not is_start:
            db.ensure_user(user.id, user.username, user.first_name or "")
        if user.id != ADMIN_ID:
            if db.is_banned(user.id):
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return None
            t = time.monotonic()
            if t - self.last.get(user.id, 0) < self.delay:
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return None
            self.last[user.id] = t
            if len(self.last) > 50000:
                self.last.clear()
        return await handler(event, data)


async def clear_state_user(handler, event, data):
    # шаги покупки и tiktok используют FSM-данные, поэтому их не сбрасываем
    if not (event.data or "").startswith(("buyt:", "buyq:", "tt:")):
        await data["state"].clear()
    return await handler(event, data)


async def clear_state_admin(handler, event, data):
    d = event.data or ""
    if d.startswith("adm") and d != "adm:adpost":
        await data["state"].clear()
    return await handler(event, data)


router.callback_query.middleware(clear_state_user)
admin_router.callback_query.middleware(clear_state_admin)


# ============================================================ ПОЛЬЗОВАТЕЛИ
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if not message.from_user:
        return
    referrer = None
    arg = (command.args or "").strip()
    if arg.startswith("ref_") and arg[4:].isdigit() and len(arg) <= 24:
        referrer = int(arg[4:])
    u = message.from_user
    is_new, credited = db.register_user(u.id, u.username, u.first_name or "", referrer)
    if db.is_banned(u.id):
        return
    if is_new and credited:
        await safe_send(
            message.bot,
            credited,
            f"🎉 По твоей ссылке пришёл новый пользователь! Начислено <b>{fmt(REF_REWARD)}⭐</b>",
        )
    await message.answer(main_text(u.id), reply_markup=main_menu(u.id))


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    await show(cb, main_text(cb.from_user.id), main_menu(cb.from_user.id))


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    info = db.get_user(cb.from_user.id)
    if not info:
        return await cb.answer()
    text = (
        f"👤 <b>{escape(cb.from_user.first_name or 'Профиль')}</b>\n\n"
        f"💰 Баланс: <b>{fmt(info[0])}⭐</b>\n"
        f"✅ Выполнено заданий: <b>{db.approved_count(cb.from_user.id)}</b>\n"
        f"⏳ На проверке: <b>{db.pending_count(cb.from_user.id)}</b>\n"
        f"👥 Приглашено друзей: <b>{info[1]}</b>"
    )
    if db.withdraw_blocked(cb.from_user.id):
        text += "\n\n🔒 <b>Вывод заблокирован</b> (отписка от канала). Обратись к администратору."
    await show(cb, text, back_menu())


@router.callback_query(F.data == "invite")
async def cb_invite(cb: CallbackQuery):
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{cb.from_user.id}"
    text = (
        "👥 <b>Приглашай друзей</b>\n\n"
        f"За каждого нового пользователя — <b>{fmt(REF_REWARD)}⭐</b>.\n\n"
        f"Твоя ссылка:\n<code>{link}</code>"
    )
    await show(cb, text, back_menu())


@router.callback_query(F.data == "withdraw")
async def cb_withdraw(cb: CallbackQuery):
    uid = cb.from_user.id
    if db.withdraw_blocked(uid):
        return await show(
            cb,
            f"🔒 <b>Вывод заблокирован.</b>\n\nТы отписался от канала раньше чем через "
            f"{SUB_HOLD_DAYS} дн. после получения награды. Обратись к администратору.",
            back_menu(),
        )
    res = db.create_withdrawal(uid)
    if not res:
        info = db.get_user(uid)
        bal = info[0] if info else 0
        return await show(
            cb,
            f"❌ Минимальный вывод — <b>{fmt(MIN_WITHDRAW)}⭐</b>.\nТвой баланс: <b>{fmt(bal)}⭐</b>",
            back_menu(),
        )
    wid, amount = res
    await show(
        cb, f"✅ Заявка <b>#{wid}</b> на <b>{fmt(amount)}⭐</b> отправлена администратору.", back_menu()
    )
    u = cb.from_user
    await safe_send(
        cb.bot,
        ADMIN_ID,
        f"💸 Заявка <b>#{wid}</b>\nПользователь: {user_label(u.id, u.username, u.first_name)}\n"
        f"Сумма: <b>{fmt(amount)}⭐</b>",
        reply_markup=kb([btn("✅ Выплачено", f"wd:ok:{wid}"), btn("❌ Отклонить", f"wd:no:{wid}")]),
    )


# ---------------------------------------------------------------- задания
def task_view(tid: int, kind: str, url: str, chat_id, need_tasks: int = 2):
    """Текст и кнопки одного задания."""
    if kind == "channel":
        if chat_id:
            text = (
                f"📢 <b>Канал #{tid}</b>\n\n1. Открой канал и подпишись.\n"
                "2. Вернись сюда и нажми «Проверить подписку»."
            )
            action = btn("✅ Проверить подписку", f"chk:{tid}")
        else:
            text = (
                f"📢 <b>Канал #{tid}</b>\n\n1. Открой канал и подпишись.\n"
                "2. Нажми «Отправить скриншот» и пришли скриншот подписки."
            )
            action = btn("📸 Отправить скриншот", f"prf:{tid}")
    else:
        if need_tasks == 0:
            step2 = "2. Нажми Start (/start) в боте."
            step3 = "3. Нажми «Отправить скриншот» и пришли скриншот, что ты зашёл в бота."
        else:
            step2 = "2. Выполни в нём 2 задания." if need_tasks == 2 else "2. Выполни задания, которые предложит бот."
            step3 = "3. Нажми «Отправить скриншот» и пришли скриншот выполненных заданий."
        text = f"🤖 <b>Бот #{tid}</b>\n\n1. Открой бота по кнопке.\n{step2}\n{step3}"
        action = btn("📸 Отправить скриншот", f"prf:{tid}")
    text += f"\n\nНаграда: <b>{fmt(TASK_REWARD)}⭐</b>"
    open_btn = InlineKeyboardButton(text="🔗 Открыть", url=url)
    return text, kb([open_btn], [action], [btn("⬅️ В меню", "menu")])


async def show_earn(cb: CallbackQuery, note: str = ""):
    """Показывает ОДНО следующее задание (по порядку)."""
    uid = cb.from_user.id
    pend = db.pending_count(uid)
    prefix = f"{note}\n\n" if note else ""
    nxt = db.available_tasks(uid, limit=1)
    t = db.task_for_user(uid, nxt[0][0]) if nxt else None
    if not t:
        text = (
            prefix + "✅ <b>Задания закончились.</b>\n\n"
            "Ты выполнил все доступные задания. Новые появятся позже — заходи проверять!"
        )
        if pend:
            text += f"\n\n⏳ На проверке: <b>{pend}</b>"
        return await show(cb, text, back_menu())
    text, markup = task_view(*t)
    if pend:
        text += f"\n⏳ На проверке: <b>{pend}</b>"
    await show(cb, prefix + text, markup)


@router.callback_query(F.data == "earn")
async def cb_earn(cb: CallbackQuery):
    await show_earn(cb)


@router.callback_query(F.data.startswith("t:"))
async def cb_task(cb: CallbackQuery):
    await show_earn(cb)


@router.callback_query(F.data.startswith("chk:"))
async def cb_check(cb: CallbackQuery):
    uid = cb.from_user.id
    tid = tail_int(cb.data)
    t = db.task_for_user(uid, tid) if tid else None
    if not t or t[1] != "channel" or not t[3]:
        return await show_earn(cb, "⚠️ Это задание уже недоступно.")
    ok = await is_member(cb.bot, t[3], uid)
    if ok is None:
        return await cb.answer("Не удалось проверить подписку. Попробуй позже.", show_alert=True)
    if not ok:
        return await cb.answer("Ты ещё не подписан на канал.", show_alert=True)
    res = db.claim_auto(uid, tid)
    if res == "ok":
        return await show_earn(cb, f"✅ Подписка подтверждена! <b>+{fmt(TASK_REWARD)}⭐</b>")
    await show_earn(cb, "⚠️ Это задание уже недоступно.")


@router.callback_query(F.data.startswith("prf:"))
async def cb_proof_start(cb: CallbackQuery, state: FSMContext):
    tid = tail_int(cb.data)
    t = db.task_for_user(cb.from_user.id, tid) if tid else None
    if not t or (t[1] == "channel" and t[3]):
        return await show_earn(cb, "⚠️ Это задание уже недоступно.")
    await state.set_state(Proof.photo)
    await state.update_data(task_id=tid)
    await show(
        cb,
        f"📸 Отправь <b>скриншот</b> выполнения задания #{tid} (одним фото).",
        kb([btn("❌ Отмена", "earn")]),
    )


@router.message(Proof.photo, F.photo)
async def msg_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("task_id")
    await state.clear()
    u = message.from_user
    if not isinstance(tid, int):
        return await message.answer("Начни заново.", reply_markup=main_menu(u.id))
    file_id = message.photo[-1].file_id
    res, utid = db.submit_proof(u.id, tid, file_id)
    texts = {
        "inactive": "⚠️ Задание уже закрыто.",
        "full": "⚠️ Места в этом задании закончились.",
        "already": "⚠️ Ты уже отправлял это задание.",
        "limit": "⚠️ Слишком много заданий на проверке (максимум 3). Дождись проверки.",
        "banned": "🚫 Доступ ограничен.",
    }
    if res != "ok":
        return await message.answer(texts.get(res, "Ошибка."), reply_markup=main_menu(u.id))
    await message.answer(
        "✅ Скриншот отправлен на проверку. После одобрения звёзды придут на баланс.",
        reply_markup=kb([btn("➡️ Следующее задание", "earn")], [btn("⬅️ В меню", "menu")]),
    )
    task = db.get_task(tid)
    await send_proof_to_admin(
        message.bot, utid, u.id, u.username, u.first_name, tid, task[1] if task else "bot",
        task[2] if task else "", file_id,
    )


@router.message(Proof.photo)
async def msg_proof_wrong(message: Message):
    await message.answer("Нужно отправить именно фото (скриншот). Или /start для отмены.")


async def send_proof_to_admin(bot: Bot, utid, uid, username, first_name, tid, kind, url, file_id):
    caption = (
        f"📸 Скриншот <b>#{utid}</b>\nЗадание #{tid} ({kind_title(kind)})\n{escape(url)}\n"
        f"От: {user_label(uid, username, first_name)}"
    )
    try:
        await bot.send_photo(
            ADMIN_ID, file_id, caption=caption,
            reply_markup=kb([btn("✅ Принять", f"pf:ok:{utid}"), btn("❌ Отклонить", f"pf:no:{utid}")]),
        )
    except TelegramAPIError:
        log.exception("Не удалось отправить скриншот админу")


# ---------------------------------------------------------------- TikTok комментарии
TIKTOK_REWARD = 10   # сотых доли звезды за 1 комментарий (= 0.10⭐)
TIKTOK_TEXTS = [
    "Забрал 25⭐ тут @StarDropEarn_Bot 🔥 Реально платит!",
    "Только что вывел 25 звёзд в @StarDropEarn_Bot ⭐ Советую всем!",
    "Набрал 25⭐ за пару минут в @StarDropEarn_Bot — советую 🚀",
    "Получил свои 25⭐ в @StarDropEarn_Bot без вложений 💫",
    "Уже 25 звёзд от @StarDropEarn_Bot ✅ Быстро и просто!",
    "25⭐ пришло от @StarDropEarn_Bot 🎁 Присоединяйтесь!",
    "Вывел 25⭐ через @StarDropEarn_Bot — работает 🔥",
    "В @StarDropEarn_Bot раздают 25⭐ бесплатно ⭐ Заходите!",
    "Получил 25 бесплатных звёзд от @StarDropEarn_Bot 💎 Топ!",
    "Только что вывел 25⭐ из @StarDropEarn_Bot 🌟 Советую!",
]


@router.callback_query(F.data == "tiktok")
async def cb_tiktok(cb: CallbackQuery):
    texts = "\n\n".join(
        f"<b>{i+1}.</b> <code>{escape(t)}</code>" for i, t in enumerate(TIKTOK_TEXTS)
    )
    text = (
        "🎵 <b>Комментарии в TikTok</b>\n\n"
        f"За каждый принятый комментарий — <b>{TIKTOK_REWARD / 100:.2f}⭐</b>\n\n"
        "<b>Правила:</b>\n"
        "• Комментируй только <b>залетевшие видео</b> (от 100k просмотров)\n"
        "• Тема видео — <b>не про Telegram</b>\n"
        "• После комментария сделай скриншот и пришли его боту\n"
        "• Каждый комментарий проверяется вручную\n\n"
        "👇 <b>Нажми на текст — он скопируется. Вставь его комментарием под видео:</b>\n\n"
        + texts
    )
    await show(cb, text, kb(
        [btn("📸 Отправить скриншот", "tt:send")],
        [btn("⬅️ Назад", "menu")],
    ))


@router.callback_query(F.data == "tt:send")
async def cb_tiktok_send(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TikTok.photo)
    await show(cb, (
        "📸 Сделай скриншот видео с твоим комментарием и отправь его сюда.\n\n"
        "⚠️ На скриншоте должны быть видны:\n"
        "• Текст комментария (полностью)\n"
        "• Количество просмотров видео\n"
        "• Ник в TikTok"
    ), kb([btn("❌ Отмена", "tiktok")]))


@router.message(TikTok.photo, F.photo)
async def msg_tiktok_proof(message: Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    file_id = message.photo[-1].file_id
    if db.pending_count(u.id) >= 5:
        return await message.answer(
            "⚠️ Слишком много заданий на проверке (максимум 5). Дождись проверки.",
            reply_markup=main_menu(u.id),
        )
    utid = db.submit_tiktok(u.id, file_id)
    await message.answer(
        "✅ Скриншот отправлен на проверку. После одобрения "
        f"<b>{TIKTOK_REWARD / 100:.2f}⭐</b> придут на баланс.",
        reply_markup=main_menu(u.id),
    )
    caption = (
        f"🎵 TikTok-скриншот <b>#{utid}</b>\n"
        f"От: {user_label(u.id, u.username, u.first_name)}"
    )
    try:
        await message.bot.send_photo(
            ADMIN_ID, file_id, caption=caption,
            reply_markup=kb([
                btn("✅ Принять", f"tt:ok:{utid}"),
                btn("❌ Отклонить", f"tt:no:{utid}"),
            ]),
        )
    except TelegramAPIError:
        log.exception("Не удалось отправить TikTok-скриншот админу")


@router.message(TikTok.photo)
async def msg_tiktok_wrong(message: Message):
    await message.answer("Нужно отправить именно фото (скриншот). Или /start для отмены.")


@admin_router.callback_query(F.data.startswith("tt:ok:") | F.data.startswith("tt:no:"))
async def cb_tiktok_resolve(cb: CallbackQuery):
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("ok", "no") or not parts[2].isdigit():
        return await cb.answer()
    approve = parts[1] == "ok"
    utid = int(parts[2])
    row = db.resolve_tiktok(utid, approve)
    if not row:
        return await cb.answer("Уже обработано", show_alert=True)
    user_id = row
    try:
        msg = cb.message
        if msg:
            await msg.edit_caption(
                caption=f"{msg.caption or ''}\n\n{'✅ Принято' if approve else '❌ Отклонено'}",
                reply_markup=None,
            )
    except TelegramAPIError:
        pass
    if approve:
        await safe_send(cb.bot, user_id,
            f"✅ Комментарий TikTok принят! Начислено <b>{TIKTOK_REWARD / 100:.2f}⭐</b>")
    else:
        await safe_send(cb.bot, user_id,
            "❌ Комментарий TikTok не принят. Убедись, что на скриншоте видно: "
            "текст комментария, просмотры видео, твой ник.")
    await cb.answer("Готово")


# ---------------------------------------------------------------- покупка подписчиков
PUBLIC_RE = re.compile(r"https://t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?")


def norm_link(text) -> str | None:
    t = (text or "").strip()
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", t):
        t = "https://t.me/" + t[1:]
    elif t.startswith("t.me/"):
        t = "https://" + t
    elif t.startswith("http://t.me/"):
        t = "https://" + t[len("http://"):]
    return t if db.valid_link(t) else None


async def check_bot_rights(bot: Bot, chat_id: int) -> str:
    try:
        m = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError:
        return "Бот не видит этот канал. Добавь его администратором."
    if m.status == "creator":
        return ""
    if m.status != "administrator":
        return "Бот не администратор канала. Добавь его администратором."
    if not getattr(m, "can_invite_users", False):
        return "У бота нет права «Добавление подписчиков». Включи эту галочку и попробуй снова."
    return ""


def qty_markup() -> InlineKeyboardMarkup:
    row = [btn(f"{n}", f"buyq:{n}") for n in BUY_OPTIONS]
    return kb(row[:2], row[2:], [btn("❌ Отмена", "menu")])


async def ask_qty(message: Message, state: FSMContext, kind: str):
    await state.set_state(None)
    who = "подписчиков" if kind == "channel" else "пользователей"
    await message.answer(
        f"Сколько {who} купить?\n1 = 1⭐. Выбери пакет:", reply_markup=qty_markup()
    )


@router.callback_query(F.data == "buy")
async def cb_buy(cb: CallbackQuery):
    opts = ", ".join(str(n) for n in BUY_OPTIONS)
    await show(
        cb,
        f"🛒 <b>Купить подписчиков</b>\n\n1 подписчик = 1⭐. Пакеты: {opts}.\nКуда нужны подписчики?",
        kb(
            [btn("📢 В канал", "buy:channel"), btn("🤖 В бота", "buy:bot")],
            [btn("⬅️ Назад", "menu")],
        ),
    )


@router.callback_query(F.data.in_({"buy:channel", "buy:bot"}))
async def cb_buy_kind(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[1]
    await state.set_state(Buy.link)
    await state.update_data(kind=kind)
    if kind == "channel":
        text = (
            "📢 <b>Подписчики в канал</b>\n\n"
            f"1. Добавь бота @{escape(BOT_USERNAME)} <b>администратором</b> канала.\n"
            "2. <b>Обязательно</b> включи право «Добавление подписчиков».\n"
            "3. Отправь ссылку на канал (https://t.me/...)."
        )
    else:
        text = (
            "🤖 <b>Пользователи в бота</b>\n\n"
            "Отправь ссылку на бота (https://t.me/ИмяБота?start=...).\n"
            "Доступ боту выдавать не нужно."
        )
    await show(cb, text, kb([btn("❌ Отмена", "menu")]))


@router.message(Buy.link)
async def msg_buy_link(message: Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("kind")
    url = norm_link(message.text)
    if kind not in ("channel", "bot") or not url:
        return await message.answer("❌ Отправь ссылку вида https://t.me/... (или /start для отмены).")
    if kind == "bot":
        await state.update_data(url=url, chat_id=None)
        return await message.answer(
            "Нужно ли выполнить задания в боте, чтобы получить 0.25⭐?\n"
            "В любом случае пользователь пришлёт скриншот на проверку.",
            reply_markup=kb(
                [btn("✅ Да, нужно выполнить задания", "buyt:1")],
                [btn("▶️ Нет, достаточно зайти и нажать /start", "buyt:0")],
                [btn("❌ Отмена", "menu")],
            ),
        )
    m = PUBLIC_RE.fullmatch(url)
    if m:
        try:
            chat = await message.bot.get_chat("@" + m.group(1))
        except TelegramAPIError:
            return await message.answer("❌ Не нашёл такой канал. Проверь ссылку.")
        if chat.type != "channel":
            return await message.answer("❌ Это не канал. Отправь ссылку на канал.")
        err = await check_bot_rights(message.bot, chat.id)
        if err:
            return await message.answer(f"❌ {err}")
        await state.update_data(url=url, chat_id=chat.id, need_tasks=0)
        return await ask_qty(message, state, "channel")
    await state.update_data(url=url)
    await state.set_state(Buy.post)
    await message.answer(
        "Это закрытый канал — по такой ссылке бот не может узнать его ID.\n"
        "Перешли сюда любой пост из канала (бот уже должен быть админом с правом «Добавление подписчиков»)."
    )


@router.message(Buy.post)
async def msg_buy_post(message: Message, state: FSMContext):
    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return await message.answer("❌ Перешли именно пост из канала (или /start для отмены).")
    err = await check_bot_rights(message.bot, origin.chat.id)
    if err:
        return await message.answer(f"❌ {err}")
    await state.update_data(chat_id=origin.chat.id, need_tasks=0)
    await ask_qty(message, state, "channel")


@router.callback_query(F.data.startswith("buyt:"))
async def cb_buy_need_tasks(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("kind") != "bot" or not data.get("url"):
        await state.clear()
        return await show(cb, "⚠️ Начни заново.", kb([btn("🛒 Купить", "buy")], [btn("⬅️ В меню", "menu")]))
    await state.update_data(need_tasks=1 if cb.data == "buyt:1" else 0)
    await show(cb, "Сколько пользователей купить?\n1 = 1⭐. Выбери пакет:", qty_markup())


@router.callback_query(F.data.startswith("buyq:"))
async def cb_buy_qty(cb: CallbackQuery, state: FSMContext):
    qty = tail_int(cb.data)
    data = await state.get_data()
    kind, url = data.get("kind"), data.get("url")
    if qty not in BUY_OPTIONS or kind not in ("channel", "bot") or not url:
        await state.clear()
        return await show(cb, "⚠️ Начни заново.", kb([btn("🛒 Купить", "buy")], [btn("⬅️ В меню", "menu")]))
    oid = db.create_order(
        cb.from_user.id, kind, url, data.get("chat_id"), int(data.get("need_tasks", 0)), qty
    )
    await state.clear()
    if oid is None:
        return await show(
            cb,
            "⚠️ У тебя уже 2 заявки на проверке. Дождись решения администратора.",
            back_menu(),
        )
    what = "Канал" if kind == "channel" else "Бот"
    text = (
        f"🧾 <b>Заказ #{oid}</b>\n{what}: {escape(url)}\nКоличество: <b>{qty}</b>\n"
        f"К оплате: <b>{qty}⭐</b>\n\n"
        f"Отправь <b>{qty}⭐</b> на аккаунт {escape(PAY_ACCOUNT)} и нажми «Я оплатил»."
    )
    await show(cb, text, kb([btn("✅ Я оплатил", f"paid:{oid}")], [btn("❌ Отмена", "menu")]))


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(cb: CallbackQuery):
    oid = tail_int(cb.data)
    if not oid or not db.mark_paid(oid, cb.from_user.id):
        return await show(cb, "⚠️ Заказ недоступен или уже отправлен на проверку.", back_menu())
    await show(
        cb,
        f"✅ Заявка #{oid} отправлена администратору. После подтверждения оплаты "
        "твоя ссылка появится в заданиях.",
        back_menu(),
    )
    await notify_order_admin(cb.bot, oid)


async def notify_order_admin(bot: Bot, oid: int):
    o = db.get_order(oid)
    if not o:
        return
    _, uid, kind, url, chat_id, need, qty, _status = o
    username, first_name = db.user_names(uid)
    lines = [
        f"🛒 <b>Заявка на покупку #{oid}</b>",
        f"Пользователь: {user_label(uid, username, first_name)}",
        f"Тип: {kind_title(kind)}",
        f"Ссылка: {escape(url)}",
    ]
    if kind == "channel" and chat_id:
        try:
            title = (await bot.get_chat(chat_id)).title
        except TelegramAPIError:
            title = "?"
        lines.append(f"Канал: «{escape(title or '?')}» (<code>{chat_id}</code>) — сверь со ссылкой!")
    if kind == "bot":
        lines.append("Задания в боте: " + ("нужно выполнить" if need else "достаточно /start"))
    lines.append(f"Количество: <b>{qty}</b> → оплата <b>{qty}⭐</b> на {escape(PAY_ACCOUNT)}")
    await safe_send(
        bot,
        ADMIN_ID,
        "\n".join(lines),
        reply_markup=kb([btn("✅ Оплата пришла", f"ord:ok:{oid}"), btn("❌ Не пришла", f"ord:no:{oid}")]),
    )


# ============================================================ АДМИНКА
def admin_menu() -> InlineKeyboardMarkup:
    return kb(
        [btn("📊 Статистика", "adm:stats"), btn("📜 История выплат", "adm:hist")],
        [btn("⏳ Невыполненные выплаты", "adm:wd")],
        [btn("📸 Проверка скриншотов", "adm:proofs")],
        [btn("🛒 Заявки на покупку", "adm:orders"), btn("💰 Баланс пользователей", "adm:bal")],
        [btn("🎵 TikTok скриншоты", "adm:tiktoks")],
        [btn("➕ Добавить задание", "adm:add"), btn("📋 Задания / убрать", "adm:tasks")],
        [btn("📢 Реклама", "adm:ads"), btn("🚫 Бан / разбан", "adm:bans")],
        [btn("⬅️ В меню", "menu")],
    )


def to_panel() -> InlineKeyboardMarkup:
    return kb([btn("⬅️ В админ-панель", "adm")])


def is_cmd(m: Message) -> bool:
    return bool(m.text and m.text.startswith("/"))


async def cancel_flow(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "adm")
async def cb_adm(cb: CallbackQuery):
    await show(cb, "🛠 <b>Админ-панель</b>", admin_menu())


@admin_router.callback_query(F.data == "adm:stats")
async def cb_stats(cb: CallbackQuery):
    s = db.stats()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{s['users']}</b> (заблокировано: {s['banned']})\n"
        f"Суммарный баланс пользователей: <b>{fmt(s['balances'])}⭐</b>\n"
        f"Невыполненных выплат: <b>{s['wd_pending'][0]}</b> на <b>{fmt(s['wd_pending'][1])}⭐</b>\n"
        f"Выплачено: <b>{s['wd_paid'][0]}</b> на <b>{fmt(s['wd_paid'][1])}⭐</b>\n"
        f"Активных заданий: <b>{s['tasks']}</b>\n"
        f"Скриншотов на проверке: <b>{s['proofs']}</b>\n"
        f"Заявок на покупку: <b>{s['orders']}</b>\n"
        f"TikTok на проверке: <b>{s.get('tiktoks', 0)}</b>\n"
        f"С заблокированным выводом: <b>{s['blocked']}</b>"
    )
    await show(cb, text, to_panel())


@admin_router.callback_query(F.data == "adm:hist")
async def cb_hist(cb: CallbackQuery):
    rows = db.withdrawal_history(20)
    if not rows:
        return await show(cb, "📜 История выплат пуста.", to_panel())
    lines = ["📜 <b>История выплат</b> (последние 20)\n"]
    for wid, uid, username, amount, status, resolved in rows:
        who = f"@{escape(username)}" if username else f"<code>{uid}</code>"
        mark = "✅" if status == "paid" else "❌"
        lines.append(f"{mark} #{wid} · {who} · <b>{fmt(amount)}⭐</b> · {tm(resolved)}")
    await show(cb, "\n".join(lines), to_panel())


@admin_router.callback_query(F.data == "adm:wd")
async def cb_pending_wd(cb: CallbackQuery):
    rows = db.pending_withdrawals(10)
    if not rows:
        return await show(cb, "⏳ Невыполненных выплат нет.", to_panel())
    await show(cb, f"⏳ Невыполненных выплат показано: <b>{len(rows)}</b> (сообщения ниже).", to_panel())
    for wid, uid, username, first_name, amount, created in rows:
        await safe_send(
            cb.bot,
            ADMIN_ID,
            f"💸 Заявка <b>#{wid}</b> от {tm(created)}\nПользователь: "
            f"{user_label(uid, username, first_name)}\nСумма: <b>{fmt(amount)}⭐</b>",
            reply_markup=kb([btn("✅ Выплачено", f"wd:ok:{wid}"), btn("❌ Отклонить", f"wd:no:{wid}")]),
        )


@admin_router.callback_query(F.data.startswith("wd:"))
async def cb_wd_resolve(cb: CallbackQuery):
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("ok", "no") or not parts[2].isdigit():
        return await cb.answer()
    approve = parts[1] == "ok"
    wid = int(parts[2])
    row = db.resolve_withdrawal(wid, approve)
    if not row:
        return await cb.answer("Заявка уже обработана", show_alert=True)
    user_id, amount = row
    await mark_done(cb, "✅ Выплачено" if approve else "❌ Отклонено")
    if approve:
        await safe_send(cb.bot, user_id, f"✅ Выплата <b>{fmt(amount)}⭐</b> по заявке #{wid} отправлена!")
    else:
        await safe_send(
            cb.bot, user_id, f"❌ Заявка #{wid} отклонена, <b>{fmt(amount)}⭐</b> возвращены на баланс."
        )
    await cb.answer("Готово")


@admin_router.callback_query(F.data == "adm:proofs")
async def cb_proofs(cb: CallbackQuery):
    rows = db.pending_proofs(5)
    if not rows:
        return await show(cb, "📸 Скриншотов на проверке нет.", to_panel())
    await show(cb, f"📸 Показано скриншотов: <b>{len(rows)}</b> (сообщения ниже).", to_panel())
    for utid, uid, username, first_name, tid, kind, url, file_id in rows:
        await send_proof_to_admin(cb.bot, utid, uid, username, first_name, tid, kind, url, file_id)


@admin_router.callback_query(F.data.startswith("pf:"))
async def cb_proof_resolve(cb: CallbackQuery):
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("ok", "no") or not parts[2].isdigit():
        return await cb.answer()
    approve = parts[1] == "ok"
    row = db.resolve_proof(int(parts[2]), approve)
    if not row:
        return await cb.answer("Уже обработано", show_alert=True)
    user_id, tid = row
    await mark_done(cb, "✅ Принято" if approve else "❌ Отклонено")
    if approve:
        await safe_send(
            cb.bot, user_id, f"✅ Задание #{tid} принято! Начислено <b>{fmt(TASK_REWARD)}⭐</b>"
        )
    else:
        await safe_send(cb.bot, user_id, f"❌ Задание #{tid} не принято. Попробуй выполнить его заново.")
    await cb.answer("Готово")


# ---------------------------------------------------------------- TikTok-скриншоты (админ)
@admin_router.callback_query(F.data == "adm:tiktoks")
async def cb_adm_tiktoks(cb: CallbackQuery):
    rows = db.pending_tiktoks(10)
    if not rows:
        return await show(cb, "🎵 TikTok-скриншотов на проверке нет.", to_panel())
    await show(cb, f"🎵 TikTok-скриншотов: <b>{len(rows)}</b> (сообщения ниже).", to_panel())
    for utid, uid, username, first_name, file_id in rows:
        caption = (
            f"🎵 TikTok-скриншот <b>#{utid}</b>\n"
            f"От: {user_label(uid, username, first_name)}"
        )
        try:
            await cb.bot.send_photo(
                ADMIN_ID, file_id, caption=caption,
                reply_markup=kb([
                    btn("✅ Принять", f"tt:ok:{utid}"),
                    btn("❌ Отклонить", f"tt:no:{utid}"),
                ]),
            )
        except TelegramAPIError:
            log.exception("Не удалось отправить TikTok-скриншот")


# ---------------------------------------------------------------- заявки на покупку
@admin_router.callback_query(F.data == "adm:orders")
async def cb_orders(cb: CallbackQuery):
    ids = db.waiting_orders(10)
    if not ids:
        return await show(cb, "🛒 Заявок на покупку нет.", to_panel())
    await show(cb, f"🛒 Заявок на проверке: <b>{len(ids)}</b> (сообщения ниже).", to_panel())
    for oid in ids:
        await notify_order_admin(cb.bot, oid)


@admin_router.callback_query(F.data.startswith("ord:"))
async def cb_order_resolve(cb: CallbackQuery):
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("ok", "no") or not parts[2].isdigit():
        return await cb.answer()
    oid = int(parts[2])
    if parts[1] == "ok":
        res = db.approve_order(oid)
        if not res:
            return await cb.answer("Заявка уже обработана", show_alert=True)
        user_id, tid, qty, kind, url, merged = res
        note = f"лимит задания #{tid} увеличен на {qty}" if merged else f"добавлено задание #{tid}"
        await mark_done(cb, f"✅ Оплата подтверждена, {note}")
        await safe_send(
            cb.bot,
            user_id,
            f"✅ Оплата по заказу #{oid} подтверждена! Твоя ссылка добавлена в задания "
            f"({qty} {'подписчиков' if kind == 'channel' else 'пользователей'}).",
        )
    else:
        user_id = db.reject_order(oid)
        if user_id is None:
            return await cb.answer("Заявка уже обработана", show_alert=True)
        await mark_done(cb, "❌ Оплата не подтверждена")
        await safe_send(
            cb.bot,
            user_id,
            f"❌ Оплата по заказу #{oid} не подтверждена. Если ты платил — напиши администратору.",
        )
    await cb.answer("Готово")


# ---------------------------------------------------------------- баланс пользователей
@admin_router.callback_query(F.data == "adm:bal")
async def cb_bal(cb: CallbackQuery):
    await show(
        cb,
        "💰 <b>Баланс пользователей</b>",
        kb(
            [btn("➕ Выдать звёзды", "adm:balg"), btn("➖ Забрать звёзды", "adm:balt")],
            [btn("🔓 Снять блок вывода", "adm:balu")],
            [btn("⬅️ В админ-панель", "adm")],
        ),
    )


@admin_router.callback_query(F.data.in_({"adm:balg", "adm:balt", "adm:balu"}))
async def cb_bal_start(cb: CallbackQuery, state: FSMContext):
    mode = {"adm:balg": "give", "adm:balt": "take", "adm:balu": "unblock"}[cb.data]
    await state.set_state(BalForm.uid)
    await state.update_data(mode=mode)
    await show(cb, "Отправь ID пользователя (число):", kb([btn("❌ Отмена", "adm:bal")]))


@admin_router.message(BalForm.uid)
async def msg_bal_uid(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    text = (message.text or "").strip()
    if not text.isdigit() or len(text) > 15:
        return await message.answer("❌ Нужен числовой ID.")
    uid = int(text)
    info = db.get_user(uid)
    if not info:
        return await message.answer("❌ Пользователь не найден. Отправь другой ID.")
    mode = (await state.get_data()).get("mode")
    if mode == "unblock":
        await state.clear()
        db.set_withdraw_block(uid, False)
        await message.answer(f"🔓 Вывод разблокирован: <code>{uid}</code>", reply_markup=admin_menu())
        await safe_send(message.bot, uid, "🔓 Администратор снял блок вывода.")
        return
    await state.update_data(uid=uid)
    await state.set_state(BalForm.amount)
    verb = "выдать" if mode == "give" else "забрать"
    await message.answer(
        f"Баланс <code>{uid}</code>: <b>{fmt(info[0])}⭐</b>\nСколько звёзд {verb}? (например 10 или 0.5)"
    )


@admin_router.message(BalForm.amount)
async def msg_bal_amount(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    amount = db.parse_amount(message.text or "")
    if amount is None:
        return await message.answer("❌ Введи положительное число (до 2 знаков после запятой).")
    data = await state.get_data()
    uid, mode = data.get("uid"), data.get("mode")
    await state.clear()
    if not isinstance(uid, int) or mode not in ("give", "take"):
        return await message.answer("❌ Начни заново.", reply_markup=admin_menu())
    status, bal = db.adjust_balance(uid, amount if mode == "give" else -amount)
    if status == "nouser":
        return await message.answer("❌ Пользователь не найден.", reply_markup=admin_menu())
    if status == "insufficient":
        return await message.answer(
            f"❌ У пользователя только {fmt(bal)}⭐, забрать {fmt(amount)}⭐ нельзя.",
            reply_markup=admin_menu(),
        )
    if mode == "give":
        await message.answer(
            f"✅ Выдано <b>{fmt(amount)}⭐</b>. Баланс <code>{uid}</code>: <b>{fmt(bal)}⭐</b>",
            reply_markup=admin_menu(),
        )
        await safe_send(message.bot, uid, f"🎁 Администратор начислил тебе <b>{fmt(amount)}⭐</b>.")
    else:
        await message.answer(
            f"✅ Списано <b>{fmt(amount)}⭐</b>. Баланс <code>{uid}</code>: <b>{fmt(bal)}⭐</b>",
            reply_markup=admin_menu(),
        )
        await safe_send(message.bot, uid, f"➖ Администратор списал с твоего баланса <b>{fmt(amount)}⭐</b>.")


@admin_router.callback_query(F.data.startswith("ub:"))
async def cb_unblock(cb: CallbackQuery):
    uid = tail_int(cb.data)
    if not uid or not db.set_withdraw_block(uid, False):
        return await cb.answer("Пользователь не найден", show_alert=True)
    await mark_done(cb, "🔓 Вывод разблокирован")
    await safe_send(cb.bot, uid, "🔓 Администратор снял блок вывода.")
    await cb.answer("Готово")


# ---------------------------------------------------------------- бан
@admin_router.callback_query(F.data == "adm:bans")
async def cb_bans(cb: CallbackQuery):
    await show(
        cb,
        "🚫 <b>Бан / разбан</b>",
        kb([btn("🚫 Забанить", "adm:ban"), btn("♻️ Разбанить", "adm:unban")], [btn("⬅️ В админ-панель", "adm")]),
    )


@admin_router.callback_query(F.data.in_({"adm:ban", "adm:unban"}))
async def cb_ban_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BanForm.uid)
    await state.update_data(ban=cb.data == "adm:ban")
    await show(cb, "Отправь ID пользователя (число):", kb([btn("❌ Отмена", "adm")]))


@admin_router.message(BanForm.uid)
async def msg_ban(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    text = (message.text or "").strip()
    if not text.isdigit() or len(text) > 15:
        return await message.answer("❌ Нужен числовой ID.")
    uid = int(text)
    if uid == ADMIN_ID:
        return await message.answer("❌ Админа банить нельзя.")
    ban = bool((await state.get_data()).get("ban"))
    await state.clear()
    ok = db.set_ban(uid, ban)
    await message.answer(
        (("🚫 Забанен" if ban else "♻️ Разбанен") + f": <code>{uid}</code>") if ok else "❌ Пользователь не найден.",
        reply_markup=admin_menu(),
    )


# ---------------------------------------------------------------- добавить / убрать задание
@admin_router.callback_query(F.data == "adm:add")
async def cb_add(cb: CallbackQuery):
    await show(
        cb,
        "➕ <b>Что добавляем?</b>",
        kb(
            [btn("📢 Канал", "adm:addk:channel"), btn("🤖 Бот", "adm:addk:bot")],
            [btn("⬅️ В админ-панель", "adm")],
        ),
    )


@admin_router.callback_query(F.data.in_({"adm:addk:channel", "adm:addk:bot"}))
async def cb_add_kind(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[2]
    await state.set_state(AddTask.url)
    await state.update_data(kind=kind)
    await show(
        cb,
        f"{kind_title(kind)}\n\nОтправь реферальную ссылку (https://t.me/...).",
        kb([btn("❌ Отмена", "adm")]),
    )


@admin_router.message(AddTask.url)
async def msg_add_url(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    url = (message.text or "").strip()
    if not db.valid_link(url):
        return await message.answer("❌ Нужна ссылка вида https://t.me/... Отправь ещё раз.")
    if db.url_active(url):
        return await message.answer("❌ Такая ссылка уже есть в списке. Отправь другую.")
    await state.update_data(url=url)
    await state.set_state(AddTask.target)
    await message.answer("Сколько подписчиков / пользователей должно перейти? (число)")


@admin_router.message(AddTask.target)
async def msg_add_target(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    text = (message.text or "").strip()
    if not text.isdigit() or not (1 <= int(text) <= 1_000_000):
        return await message.answer("❌ Введи число от 1 до 1000000.")
    n = int(text)
    data = await state.get_data()
    if data.get("kind") == "channel":
        await state.update_data(target=n)
        await state.set_state(AddTask.chat)
        return await message.answer(
            "Для автопроверки подписки сделай бота админом канала и <b>перешли сюда любой пост</b> "
            "из этого канала.\nИли отправь «-» — тогда подписку будешь проверять по скриншотам."
        )
    tid = db.add_task(data["kind"], data["url"], n)
    await state.clear()
    if tid is None:
        return await message.answer("❌ Такая ссылка уже есть.", reply_markup=admin_menu())
    await message.answer(f"✅ Бот #{tid} добавлен, лимит: <b>{n}</b>.", reply_markup=admin_menu())


async def resolve_channel(bot: Bot, message: Message):
    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return None, "Перешли пост из канала (или отправь «-»)."
    cid = origin.chat.id
    try:
        m = await bot.get_chat_member(cid, bot.id)
    except TelegramAPIError:
        return None, "Бот не видит этот канал. Добавь его администратором и пришли пост снова."
    if m.status not in ("administrator", "creator"):
        return None, "Бот не администратор этого канала. Сделай его админом и пришли пост снова."
    return cid, ""


@admin_router.message(AddTask.chat)
async def msg_add_chat(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    data = await state.get_data()
    chat_id = None
    if isinstance(message.forward_origin, MessageOriginChannel):
        chat_id, err = await resolve_channel(message.bot, message)
        if chat_id is None:
            return await message.answer(f"❌ {err}")
    elif (message.text or "").strip() != "-":
        return await message.answer("❌ Перешли пост из канала или отправь «-».")
    tid = db.add_task("channel", data["url"], data["target"], chat_id)
    await state.clear()
    if tid is None:
        return await message.answer("❌ Такая ссылка уже есть.", reply_markup=admin_menu())
    mode = "автопроверка подписки" if chat_id else "проверка по скриншотам"
    await message.answer(
        f"✅ Канал #{tid} добавлен, лимит: <b>{data['target']}</b>, {mode}.", reply_markup=admin_menu()
    )


@admin_router.callback_query(F.data == "adm:tasks")
async def cb_tasks(cb: CallbackQuery):
    tasks = db.list_active_tasks()
    if not tasks:
        return await show(cb, "📋 Активных заданий нет.", to_panel())
    rows = []
    for tid, kind, url, chat_id, target, done in tasks[:40]:
        rows.append([btn(f"{kind_title(kind)} #{tid} · {done}/{target}", f"adm:task:{tid}")])
    rows.append([btn("⬅️ В админ-панель", "adm")])
    await show(cb, f"📋 <b>Активные задания</b> ({len(tasks)}). Нажми, чтобы открыть.", kb(*rows))


@admin_router.callback_query(F.data.startswith("adm:task:"))
async def cb_task_detail(cb: CallbackQuery):
    tid = tail_int(cb.data)
    t = db.get_task(tid) if tid else None
    if not t or t[6] == "deleted":
        return await show(cb, "Задание не найдено.", kb([btn("⬅️ К списку", "adm:tasks")]))
    _, kind, url, chat_id, target, done, status = t
    check = "автопроверка" if chat_id else "по скриншотам"
    text = (
        f"{kind_title(kind)} <b>#{tid}</b>\n{escape(url)}\n\n"
        f"Выполнено: <b>{done}/{target}</b>\nСтатус: {status}\nПроверка: {check}"
    )
    rows = [[btn("✏️ Изменить лимит", f"adm:ttgt:{tid}")]]
    if kind == "channel":
        rows.append([btn("🔗 Привязать канал (пост)", f"adm:tbind:{tid}")])
    rows.append([btn("🗑 Убрать", f"adm:tdel:{tid}")])
    rows.append([btn("⬅️ К списку", "adm:tasks")])
    await show(cb, text, kb(*rows))


@admin_router.callback_query(F.data.startswith("adm:tdel:"))
async def cb_task_del(cb: CallbackQuery):
    tid = tail_int(cb.data)
    if not tid:
        return await cb.answer()
    await show(
        cb,
        f"Убрать задание #{tid} из списка?",
        kb([btn("✅ Да, убрать", f"adm:tdelc:{tid}"), btn("↩️ Нет", f"adm:task:{tid}")]),
    )


@admin_router.callback_query(F.data.startswith("adm:tdelc:"))
async def cb_task_del_confirm(cb: CallbackQuery):
    tid = tail_int(cb.data)
    ok = bool(tid) and db.delete_task(tid)
    await show(
        cb, f"🗑 Задание #{tid} убрано." if ok else "Задание не найдено.", kb([btn("⬅️ К списку", "adm:tasks")])
    )


@admin_router.callback_query(F.data.startswith("adm:ttgt:"))
async def cb_task_target(cb: CallbackQuery, state: FSMContext):
    tid = tail_int(cb.data)
    if not tid or not db.get_task(tid):
        return await cb.answer()
    await state.set_state(EditTarget.value)
    await state.update_data(tid=tid)
    await show(cb, f"Новый лимит для задания #{tid} (число):", kb([btn("❌ Отмена", "adm:tasks")]))


@admin_router.message(EditTarget.value)
async def msg_edit_target(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    text = (message.text or "").strip()
    if not text.isdigit() or not (1 <= int(text) <= 1_000_000):
        return await message.answer("❌ Введи число от 1 до 1000000.")
    tid = (await state.get_data()).get("tid")
    await state.clear()
    ok = isinstance(tid, int) and db.set_target(tid, int(text))
    await message.answer("✅ Лимит обновлён." if ok else "❌ Задание не найдено.", reply_markup=admin_menu())


@admin_router.callback_query(F.data.startswith("adm:tbind:"))
async def cb_task_bind(cb: CallbackQuery, state: FSMContext):
    tid = tail_int(cb.data)
    t = db.get_task(tid) if tid else None
    if not t or t[1] != "channel":
        return await cb.answer()
    await state.set_state(Bind.post)
    await state.update_data(tid=tid)
    await show(
        cb,
        f"Сделай бота админом канала #{tid} и перешли сюда любой пост из него.",
        kb([btn("❌ Отмена", "adm:tasks")]),
    )


@admin_router.message(Bind.post)
async def msg_bind(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    chat_id, err = await resolve_channel(message.bot, message)
    if chat_id is None:
        return await message.answer(f"❌ {err}")
    tid = (await state.get_data()).get("tid")
    await state.clear()
    ok = isinstance(tid, int) and db.set_chat_id(tid, chat_id)
    await message.answer(
        "✅ Канал привязан, включена автопроверка подписки." if ok else "❌ Задание не найдено.",
        reply_markup=admin_menu(),
    )


# ---------------------------------------------------------------- реклама
async def broadcast_ad(bot: Bot, ad_id: int, src_chat: int, src_msg: int):
    users = db.all_user_ids()
    sem = asyncio.Semaphore(5)
    sent: list[tuple[int, int]] = []
    fails = 0

    async def one(uid: int):
        nonlocal fails
        async with sem:
            for _ in range(2):
                try:
                    m = await bot.copy_message(uid, src_chat, src_msg)
                    sent.append((uid, m.message_id))
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except TelegramAPIError:
                    fails += 1
                    break
            else:
                fails += 1
            await asyncio.sleep(0.2)

    await asyncio.gather(*[one(u) for u in users], return_exceptions=True)
    db.record_ad_messages(ad_id, sent)
    return len(sent), fails


async def purge_ad(bot: Bot, ad_id: int):
    for chat_id, mid in db.ad_messages(ad_id):
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramAPIError:
            pass
        await asyncio.sleep(0.04)
    db.mark_ad_deleted(ad_id)


async def ad_expiry_loop(bot: Bot):
    while True:
        await asyncio.sleep(20)
        try:
            for ad_id in db.expired_ads():
                await purge_ad(bot, ad_id)
                log.info("Реклама #%s истекла и удалена", ad_id)
        except Exception:
            log.exception("ad_expiry_loop")


@admin_router.callback_query(F.data == "adm:ads")
async def cb_ads(cb: CallbackQuery):
    ads = db.active_ads()
    rows = [[btn(f"🗑 Удалить #{aid} (до {tm(exp)})", f"adm:addel:{aid}")] for aid, exp in ads]
    rows.append([btn("➕ Новая реклама", "adm:adnew")])
    rows.append([btn("⬅️ В админ-панель", "adm")])
    text = f"📢 <b>Реклама</b>\n\nАктивных: <b>{len(ads)}</b>"
    await show(cb, text, kb(*rows))


@admin_router.callback_query(F.data == "adm:adnew")
async def cb_ad_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdForm.content)
    await show(
        cb,
        "Отправь сообщение для рекламы (текст, фото или видео — как оно должно выглядеть).",
        kb([btn("❌ Отмена", "adm:ads")]),
    )


@admin_router.message(AdForm.content)
async def msg_ad_content(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    if not (message.text or message.photo or message.video):
        return await message.answer("❌ Отправь текст, фото или видео.")
    await state.update_data(src_chat=message.chat.id, src_msg=message.message_id)
    await state.set_state(AdForm.duration)
    await message.answer("Укажи время показа, например: <code>1д 2ч 30м</code> (максимум 30д).")


@admin_router.message(AdForm.duration)
async def msg_ad_duration(message: Message, state: FSMContext):
    if is_cmd(message):
        return await cancel_flow(message, state)
    sec = db.parse_duration(message.text or "")
    if not sec:
        return await message.answer("❌ Неверный формат. Пример: <code>1д 2ч 30м</code>")
    data = await state.get_data()
    await state.update_data(duration=sec)
    try:
        await message.bot.copy_message(message.chat.id, data["src_chat"], data["src_msg"])
    except TelegramAPIError:
        await state.clear()
        return await message.answer("❌ Не удалось показать превью. Начни заново.", reply_markup=admin_menu())
    n = len(db.all_user_ids())
    await message.answer(
        f"Выше — превью. Разослать <b>{n}</b> пользователям на <b>{db.fmt_duration(sec)}</b>? "
        "По истечении времени сообщения удалятся.",
        reply_markup=kb([btn("🚀 Разослать", "adm:adpost")], [btn("❌ Отмена", "adm")]),
    )


@admin_router.callback_query(F.data == "adm:adpost")
async def cb_ad_post(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if not all(k in data for k in ("src_chat", "src_msg", "duration")):
        return await show(cb, "Нет данных рекламы. Создай её заново.", to_panel())
    await show(cb, "⏳ Рассылаю...")
    ad_id = db.create_ad(data["duration"])
    started = time.monotonic()
    ok, fail = await broadcast_ad(cb.bot, ad_id, data["src_chat"], data["src_msg"])
    await safe_send(
        cb.bot,
        ADMIN_ID,
        f"✅ Реклама <b>#{ad_id}</b> разослана.\nУспешно: {ok}\nОшибок: {fail}\n"
        f"Время: {time.monotonic() - started:.1f} с\nСрок показа: {db.fmt_duration(data['duration'])}",
        reply_markup=admin_menu(),
    )


@admin_router.callback_query(F.data.startswith("adm:addel:"))
async def cb_ad_delete(cb: CallbackQuery):
    ad_id = tail_int(cb.data)
    if not ad_id:
        return await cb.answer()
    await show(cb, "⏳ Удаляю рекламу у пользователей...")
    await purge_ad(cb.bot, ad_id)
    await safe_send(cb.bot, ADMIN_ID, f"🗑 Реклама #{ad_id} удалена.", reply_markup=admin_menu())


# ============================================================ ФОНОВЫЕ ЗАДАЧИ
async def sub_check_loop(bot: Bot):
    while True:
        await asyncio.sleep(10)
        try:
            for utid, uid, tid, chat_id in db.due_sub_checks(20):
                ok = await is_member(bot, chat_id, uid)
                if ok is False:
                    if db.mark_unsub(utid):
                        username, first_name = db.user_names(uid)
                        await safe_send(
                            bot, uid,
                            f"🔒 Ты отписался от канала #{tid} раньше чем через {SUB_HOLD_DAYS} дн. "
                            "после получения награды. Вывод звёзд заблокирован. "
                            "Если это ошибка — напиши администратору.",
                        )
                        await safe_send(
                            bot, ADMIN_ID,
                            f"🔒 {user_label(uid, username, first_name)} отписался от канала #{tid} "
                            f"в течение {SUB_HOLD_DAYS} дн. Вывод заблокирован.",
                            reply_markup=kb([btn("🔓 Снять блок", f"ub:{uid}")]),
                        )
                else:
                    db.touch_check(utid)
                await asyncio.sleep(0.1)
        except Exception:
            log.exception("sub_check_loop")


# ============================================================ ЗАПУСК
async def main():
    global BOT_USERNAME
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    log.info("Токен из .env: %s…%s (длина %s)", BOT_TOKEN[:10], BOT_TOKEN[-4:], len(BOT_TOKEN))
    try:
        BOT_USERNAME = (await bot.get_me()).username
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SystemExit(
            "\n❌ Telegram отклонил токен (Unauthorized).\n"
            "Открой @BotFather → /mybots → твой бот → API Token, скопируй актуальный токен "
            "и вставь в файл .env в строку BOT_TOKEN=... (без пробелов и кавычек)."
        )
    except TelegramNetworkError:
        await bot.session.close()
        raise SystemExit("\n❌ Нет связи с Telegram. Проверь интернет / VPN и запусти снова.")
    updated = db.apply_known_chat_ids()
    if updated:
        log.info("Привязано chat_id к каналам: %s", updated)
    for tid, kind, url, chat_id, _t, _d in db.list_active_tasks():
        if kind == "channel" and chat_id:
            try:
                member = await bot.get_chat_member(chat_id, bot.id)
                chat = await bot.get_chat(chat_id)
                log.info("Канал #%s «%s»: статус бота — %s", tid, chat.title, member.status)
                if member.status not in ("administrator", "creator"):
                    log.warning("Канал #%s: бот НЕ админ, проверка подписки не заработает", tid)
            except TelegramAPIError:
                log.warning("Канал #%s: бот не видит chat_id %s (проверь, что он админ)", tid, chat_id)
    added = db.seed_tasks()
    if added:
        log.info("Добавлено стартовых заданий: %s", added)
    dp = Dispatcher()
    guard = Guard()
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(admin_router)
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    ad_task = asyncio.create_task(ad_expiry_loop(bot))
    sub_task = asyncio.create_task(sub_check_loop(bot))
    log.info("Бот @%s запущен", BOT_USERNAME)
    try:
        await dp.start_polling(bot)
    finally:
        ad_task.cancel()
        sub_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
