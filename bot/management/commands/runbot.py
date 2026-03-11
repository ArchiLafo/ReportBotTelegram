import asyncio
import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from asgiref.sync import sync_to_async

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command as AiogramCommand
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ChatMemberUpdated, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from core.models import (
    TgUser, TgRole, KnownChat, KnownChatType,
    OrgObject, Well, WellStatus, ObjectStatus,
    Form, FormField, Report, CloseRequest, CloseRequestStatus,
    WellStage
)

logger = logging.getLogger(__name__)

# --------------------- Состояния FSM ---------------------
class CreateObjectStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_chat = State()
    waiting_for_wells = State()


class ReportStates(StatesGroup):
    choosing_report_type = State()
    choosing_object = State()
    choosing_stage = State()
    choosing_well = State()
    entering_fields = State()
    asking_accident = State()
    accident_handling = State()
    confirm_send = State()
    sending_files = State()      
    waiting_temperature = State()


# ------------------------------------------------------------
#  Вспомогательные функции для работы с БД
# ------------------------------------------------------------
@sync_to_async
def get_or_create_user(tg_user_id: int, full_name: str) -> TgUser:
    user, created = TgUser.objects.get_or_create(
        tg_user_id=tg_user_id,
        defaults={
            'full_name': full_name,
            'role': TgRole.FILLER,
        }
    )
    if not created and full_name and user.full_name != full_name:
        user.full_name = full_name
        user.save(update_fields=['full_name'])
    return user


@sync_to_async
def get_user_role(tg_user_id: int) -> str:
    try:
        user = TgUser.objects.get(tg_user_id=tg_user_id)
        return user.role
    except TgUser.DoesNotExist:
        return TgRole.FILLER


@sync_to_async
def upsert_known_chat(chat_id: int, title: str, chat_type: str, is_active: bool) -> None:
    if chat_type not in ('group', 'supergroup'):
        return
    KnownChat.objects.update_or_create(
        chat_id=chat_id,
        defaults={
            'title': title or '',
            'chat_type': chat_type,
            'is_active': is_active,
            'last_seen_at': timezone.now(),
        }
    )


async def check_user_role(message: Message, allowed_roles: list) -> bool:
    user_id = message.from_user.id
    try:
        user = await sync_to_async(TgUser.objects.get)(tg_user_id=user_id)
        if user.role in allowed_roles:
            return True
        await message.answer("⛔ У вас нет прав для выполнения этой команды.")
        return False
    except TgUser.DoesNotExist:
        await message.answer("Сначала выполните /start для регистрации.")
        return False


# @sync_to_async
# def get_active_objects():
#     from django.db.models import Exists, OuterRef
#     active_wells = Well.objects.filter(object=OuterRef('pk'), status=WellStatus.ACTIVE)
#     return list(OrgObject.objects.filter(status=ObjectStatus.ACTIVE).annotate(
#         has_active_wells=Exists(active_wells)
#     ).filter(has_active_wells=True).order_by('name'))
@sync_to_async
def get_active_objects():
    from django.db.models import Exists, OuterRef
    active_wells = Well.objects.filter(object=OuterRef('pk'), status=WellStatus.ACTIVE)
    return list(OrgObject.objects.filter(status=ObjectStatus.ACTIVE)
                .annotate(has_active_wells=Exists(active_wells))
                .filter(has_active_wells=True)
                .select_related('chat')   # добавляем предзагрузку чата
                .order_by('name'))


@sync_to_async
def get_stages_with_wells(object_id):
    stages = Well.objects.filter(
        object_id=object_id,
        status=WellStatus.ACTIVE
    ).values_list('stage', flat=True)
    unique_stages = list(set(stages))
    return unique_stages


@sync_to_async
def get_wells_by_stage(object_id, stage):
    return list(Well.objects.filter(
        object_id=object_id,
        stage=stage,
        status=WellStatus.ACTIVE
    ).order_by('name'))


@sync_to_async
def get_form_fields(form_code: str):
    try:
        form = Form.objects.get(code=form_code, is_active=True)
        return list(form.fields.filter(is_active=True).order_by('order_index'))
    except Form.DoesNotExist:
        return []


def compose_report_text(report_date, author_name, well_name, stage, answers, fields, accident_data=None):
    lines = []
    lines.append(f"{report_date.strftime('%d.%m.%Y')} — {author_name}")
    lines.append(f"Скважина: {well_name}")
    lines.append(f"Этап: {WellStage(stage).label}")

    if accident_data and accident_data.get('occurred'):
        if accident_data['type'] == 'repairable':
            lines.append("⚠️ Аварийная ситуация (устраняется)")
        elif accident_data['type'] == 'fatal':
            lines.append("❌ Аварийная ситуация (скважина закрыта)")
        elif accident_data['type'] == 'technical':
            lines.append("⚠️ Техническая авария (время обнулено)")
        elif accident_data['type'] == 'successful':
            lines.append("⚠️ Результативная авария (этап завершён досрочно)")
        else:
            lines.append("⚠️ Аварийная ситуация")
    else:
        lines.append("✅ Без аварий")

    if fields:
        lines.append("")
        for f in fields:
            val = answers.get(f.key)
            if val is None:
                continue
            if f.type == 'checkbox':
                val = 'Да' if val else 'Нет'
            lines.append(f"{f.label}: {val}")

    return "\n".join(lines).strip()


async def transition_well_stage(well, new_stage, bot=None):
    old_stage = well.stage
    well.stage = new_stage
    now = timezone.now()

    if new_stage == WellStage.PUMPING:
        well.pumping_started_at = now
    elif new_stage == WellStage.MODE:
        well.mode_started_at = now
    elif new_stage == WellStage.LIQUIDATION:
        pass
    elif new_stage == WellStage.COMPLETED:
        well.closed_at = now
        well.status = WellStatus.CLOSED
        well.closed_reason = 'completed'

    await sync_to_async(well.save)(update_fields=[
        'stage', 'pumping_started_at', 'mode_started_at', 'closed_at', 'status', 'closed_reason'
    ])

    if bot and well.object and well.object.chat:
        text = f"🔁 Скважина {well.name} перешла с этапа «{WellStage(old_stage).label}» на этап «{WellStage(new_stage).label}»."
        if new_stage == WellStage.COMPLETED:
            text = f"✅ Скважина {well.name} полностью завершена."
        try:
            await bot.send_message(chat_id=well.object.chat.chat_id, text=text)
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление о переходе: {e}")


# --------------------- Хендлеры команд ---------------------
async def cmd_start(message: Message) -> None:
    user = message.from_user
    full_name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    db_user = await get_or_create_user(user.id, full_name)

    role_display = dict(TgRole.choices).get(db_user.role, 'неизвестна')
    text = (
        f"👋 Привет, {full_name}!\n"
        f"Твоя роль: {role_display}\n\n"
        "Используй /help для списка команд."
    )
    await message.answer(text)


async def cmd_help(message: Message) -> None:
    text = (
        "Доступные команды:\n"
        "/start - начало работы\n"
        "/help - эта справка\n"
        "/cancel - отменить текущее действие\n"
        "/report - создать ежедневную сводку\n"
        "/create_object - создать новый объект (админ/разработчик)\n"
    )
    await message.answer(text)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активного действия.")
        return
    await state.clear()
    await message.answer("Действие отменено.")


# --------------------- Создание объекта (админ) ---------------------
async def cmd_create_object(message: Message, state: FSMContext) -> None:
    if not await check_user_role(message, [TgRole.ADMIN, TgRole.DEVELOPER]):
        return
    await message.answer("Введите название нового объекта:")
    await state.set_state(CreateObjectStates.waiting_for_name)


async def process_object_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым. Введите название:")
        return
    await state.update_data(object_name=name)

    chats = await sync_to_async(list)(
        KnownChat.objects.filter(is_active=True, bound_object__isnull=True)
    )
    if not chats:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить (привязать позже)", callback_data="skip_chat")]
        ])
        await message.answer(
            "Нет доступных чатов для привязки. Вы можете привязать объект к чату позже.",
            reply_markup=kb
        )
        await state.set_state(CreateObjectStates.waiting_for_chat)
        return

    buttons = []
    for chat in chats:
        title = chat.title or f"Чат {chat.chat_id}"
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"chat_{chat.chat_id}")])
    buttons.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_chat")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выберите чат для привязки объекта или пропустите:", reply_markup=kb)
    await state.set_state(CreateObjectStates.waiting_for_chat)


async def process_chat_choice(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data == "skip_chat":
        await state.update_data(chat_id=None)
        await callback.message.edit_text("Чат не выбран. Переходим к добавлению скважин.")
    else:
        chat_id = int(callback.data.split("_")[1])
        chat = await sync_to_async(
            KnownChat.objects.filter(chat_id=chat_id, is_active=True, bound_object__isnull=True).first
        )()
        if not chat:
            await callback.message.edit_text("Этот чат уже недоступен. Попробуйте снова.")
            await state.clear()
            return
        await state.update_data(chat_id=chat_id)
        await callback.message.edit_text("Чат выбран. Переходим к добавлению скважин.")

    await callback.message.answer(
        "Введите данные скважины одной строкой:\n"
        "<b>Название Глубина [Часы_ОФР] [Количество_режимов]</b>\n"
        "Пример: Скв-1 150 24 3\n"
        "Для завершения введите /done"
    )
    await state.set_state(CreateObjectStates.waiting_for_wells)


async def process_well(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        await message.answer("Введите как минимум название и глубину.")
        return
    name = parts[0]
    try:
        depth = float(parts[1].replace(',', '.'))
    except ValueError:
        await message.answer("Глубина должна быть числом.")
        return

    pumping_hours = None
    mode_count = 0
    if len(parts) >= 3:
        try:
            pumping_hours = float(parts[2].replace(',', '.'))
        except ValueError:
            await message.answer("Часы ОФР должны быть числом.")
            return
    if len(parts) >= 4:
        try:
            mode_count = int(parts[3])
        except ValueError:
            await message.answer("Количество режимов должно быть целым числом.")
            return

    data = await state.get_data()
    wells = data.get('wells', [])
    wells.append({
        'name': name,
        'depth': depth,
        'pumping_hours': pumping_hours,
        'mode_count': mode_count,
    })
    await state.update_data(wells=wells)

    await message.answer(
        f"Скважина {name} добавлена.\n"
        "Можете добавить ещё или введите /done для завершения."
    )


async def finish_wells(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    object_name = data.get('object_name')
    obj = await sync_to_async(OrgObject.objects.create)(
        name=object_name,
        status=ObjectStatus.ACTIVE
    )
    chat_id = data.get('chat_id')
    wells_data = data.get('wells', [])

    if chat_id:
        existing_obj = await sync_to_async(OrgObject.objects.filter(chat_id=chat_id).first)()
        if existing_obj:
            await message.answer("⚠️ Внимание: выбранный чат уже привязан к другому объекту. Объект создан без привязки к чату.")
        else:
            chat = await sync_to_async(KnownChat.objects.get)(chat_id=chat_id)
            obj.chat = chat
            await sync_to_async(obj.save)(update_fields=['chat'])

    for w in wells_data:
        await sync_to_async(Well.objects.create)(
            object=obj,
            name=w['name'],
            planned_depth_m=w['depth'],
            planned_pumping_hours=w['pumping_hours'],
            planned_mode_count=w['mode_count'],
            remaining_mode_count=w['mode_count'],
            total_pumping_hours=0.0,
            total_discharge_hours=0.0,
            total_drilling_pumping_hours=0.0,
            status=WellStatus.ACTIVE,
            stage=WellStage.DRILLING
        )

    await message.answer(f"✅ Объект «{object_name}» создан. Добавлено скважин: {len(wells_data)}")
    await state.clear()


# --------------------- Ежедневная сводка (report) ---------------------
async def cmd_report(message: Message, state: FSMContext) -> None:
    if not await check_user_role(message, [TgRole.FILLER, TgRole.ADMIN, TgRole.DEVELOPER]):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔨 Работа", callback_data="report_type_work"),
         InlineKeyboardButton(text="🌡️ Температура", callback_data="report_type_temp")]
    ])
    await message.answer("Выберите тип сводки:", reply_markup=kb)
    await state.set_state(ReportStates.choosing_report_type)


async def process_report_type(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data == "report_type_work":
        objects = await get_active_objects()
        if not objects:
            await callback.message.edit_text("Нет доступных объектов для отчёта.")
            await state.clear()
            return

        user_role = await get_user_role(callback.from_user.id)
        if user_role in [TgRole.ADMIN, TgRole.DEVELOPER]:
            filtered_objects = objects
        else:
            filtered_objects = []
            cache = {}
            for obj in objects:
                if obj.chat:
                    chat_id = obj.chat.chat_id
                    if chat_id not in cache:
                        cache[chat_id] = await is_user_in_chat(callback.bot, callback.from_user.id, chat_id)
                    if cache[chat_id]:
                        filtered_objects.append(obj)
            if not filtered_objects:
                await callback.message.edit_text("У вас нет доступа ни к одному объекту.")
                await state.clear()
                return

        await state.update_data(objects_list=filtered_objects)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=obj.name, callback_data=f"rep_obj_{obj.id}")]
            for obj in filtered_objects
        ])
        await callback.message.edit_text("Выберите объект:", reply_markup=kb)
        await state.set_state(ReportStates.choosing_object)
    elif callback.data == "report_type_temp":
        objects = await get_active_objects()
        if not objects:
            await callback.message.edit_text("Нет доступных объектов для отчёта.")
            await state.clear()
            return

        user_role = await get_user_role(callback.from_user.id)
        if user_role in [TgRole.ADMIN, TgRole.DEVELOPER]:
            filtered_objects = objects
        else:
            filtered_objects = []
            for obj in objects:
                if obj.chat and await is_user_in_chat(callback.bot, callback.from_user.id, obj.chat.chat_id):
                    filtered_objects.append(obj)
            if not filtered_objects:
                await callback.message.edit_text("У вас нет доступа ни к одному объекту.")
                await state.clear()
                return

        await state.update_data(objects_list=filtered_objects)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=obj.name, callback_data=f"temp_obj_{obj.id}")]
            for obj in filtered_objects
        ])
        await callback.message.edit_text("Выберите объект для температурной сводки:", reply_markup=kb)
        await state.set_state(ReportStates.choosing_object)


async def process_report_object(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data.startswith("rep_obj_"):
        return
    obj_id = int(callback.data.split("_")[2])
    stages = await get_stages_with_wells(obj_id)
    if not stages:
        await callback.message.edit_text("У выбранного объекта нет активных скважин.")
        await state.clear()
        return
    await state.update_data(selected_object_id=obj_id)
    buttons = []
    for stage in stages:
        label = WellStage(stage).label
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"rep_stage_{stage}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("Выберите этап:", reply_markup=kb)
    await state.set_state(ReportStates.choosing_stage)


async def process_temp_object(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data.startswith("temp_obj_"):
        return
    obj_id = int(callback.data.split("_")[2])
    await state.update_data(selected_object_id=obj_id, report_type='temperature')
    await callback.message.edit_text("Введите температуру (например: -5.5 или 10):")
    await state.set_state(ReportStates.waiting_temperature)


async def handle_temperature(message: Message, state: FSMContext) -> None:
    try:
        temp = float(message.text.strip().replace(',', '.'))
    except ValueError:
        await message.answer("Введите число (температуру).")
        return

    data = await state.get_data()
    obj_id = data['selected_object_id']
    obj = await sync_to_async(OrgObject.objects.select_related('chat').get)(id=obj_id)
    user = await sync_to_async(TgUser.objects.get)(tg_user_id=message.from_user.id)
    report_date = timezone.localdate()

    text = (
        f"{report_date.strftime('%d.%m.%Y')} — {user.full_name or user.tg_user_id}\n"
        f"Температура на объекте {obj.name} {temp}°C"
    )

    report = await sync_to_async(Report.objects.create)(
        object=obj,
        author=user,
        payload_json={'temperature': temp},
        report_date=report_date,
        message_text=text,
        stage=None,
    )

    if obj.chat:
        try:
            sent = await message.bot.send_message(chat_id=obj.chat.chat_id, text=text)
            report.tg_chat_id = sent.chat.id
            report.tg_message_id = sent.message_id
            await sync_to_async(report.save)(update_fields=['tg_chat_id', 'tg_message_id'])
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение в чат {obj.chat.chat_id}: {e}")
            await message.answer("⚠️ Отчёт сохранён, но не отправлен в чат.")
    else:
        await message.answer("⚠️ У объекта не привязан чат, сообщение никуда не отправлено.")

    await message.answer("✅ Температурная сводка отправлена.")
    await state.clear()


async def process_report_stage(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data.startswith("rep_stage_"):
        return
    stage = callback.data.split("_")[2]
    data = await state.get_data()
    obj_id = data['selected_object_id']
    wells = await get_wells_by_stage(obj_id, stage)
    if not wells:
        await callback.message.edit_text("На этом этапе нет активных скважин.")
        await state.clear()
        return
    await state.update_data(selected_stage=stage, wells_list=wells)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=w.name, callback_data=f"rep_well_{w.id}")]
        for w in wells
    ])
    await callback.message.edit_text("Выберите скважину:", reply_markup=kb)
    await state.set_state(ReportStates.choosing_well)


async def process_report_well(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data.startswith("rep_well_"):
        return
    well_id = int(callback.data.split("_")[2])
    await state.update_data(selected_well_id=well_id)

    data = await state.get_data()
    stage = data['selected_stage']

    # Получаем скважину для сохранения текущих данных
    well = await sync_to_async(Well.objects.get)(id=well_id)
    if stage == WellStage.DRILLING:
        await state.update_data(current_depth=well.current_depth_m)

    # Определяем код формы
    form_code_map = {
        WellStage.DRILLING: 'drilling_daily',
        WellStage.PUMPING: 'pumping_daily',
        WellStage.MODE: 'mode_daily',
        WellStage.LIQUIDATION: 'liquidation_daily',
    }
    form_code = form_code_map.get(stage)
    if not form_code:
        await callback.message.edit_text("Для данного этапа нет формы отчёта.")
        await state.clear()
        return

    fields = await get_form_fields(form_code)
    if not fields:
        # Если полей нет, сразу переходим к вопросу об аварии
        await ask_accident(callback.message, state)
        await callback.message.delete()
        return
    await state.update_data(form_fields=fields, current_field_index=0, answers={})
    await ask_next_field(callback.message, state)
    await callback.message.delete()


async def ask_next_field(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data['form_fields']
    idx = data['current_field_index']
    if idx >= len(fields):
        # Все поля заполнены, переходим к вопросу об аварии
        await ask_accident(message, state)
        return
    field = fields[idx]
    text = f"<b>{field.label}</b>" + (" (обязательное)" if field.required else "")
    if field.key == 'drilled_m':
        current = data.get('current_depth')
        if current is not None:
            text += f"\n<i>Текущая глубина: {current} м</i>"
        else:
            text += f"\n<i>Текущая глубина не указана</i>"
    if field.type == 'select':
        options = field.options_json
        if not options:
            await state.update_data(current_field_index=idx+1)
            await ask_next_field(message, state)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=opt, callback_data=f"fld:select:{field.key}:{i}")]
            for i, opt in enumerate(options)
        ])
        await message.answer(text, reply_markup=kb)
    elif field.type == 'checkbox':
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data=f"fld:checkbox:{field.key}:true"),
             InlineKeyboardButton(text="Нет", callback_data=f"fld:checkbox:{field.key}:false")]
        ])
        await message.answer(text, reply_markup=kb)
    else:  # text, number
        await message.answer(text)
    await state.set_state(ReportStates.entering_fields)


async def handle_field_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data['form_fields']
    idx = data['current_field_index']
    field = fields[idx]
    value = message.text.strip()
    if field.type == 'number':
        try:
            value = float(value.replace(',', '.'))
        except ValueError:
            await message.answer("Введите число.")
            return
    answers = data.get('answers', {})
    answers[field.key] = value
    await state.update_data(answers=answers, current_field_index=idx+1)
    await ask_next_field(message, state)


async def handle_field_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data.startswith("fld:"):
        return
    parts = callback.data.split(':')
    if len(parts) != 4:
        await callback.answer("Ошибка формата данных", show_alert=True)
        return
    fld_type = parts[1]
    field_key = parts[2]
    value_str = parts[3]

    data = await state.get_data()
    fields = data.get('form_fields', [])
    idx = data.get('current_field_index', 0)

    if idx >= len(fields):
        await callback.answer("Ошибка: нет активного поля", show_alert=True)
        return

    current_field = fields[idx]
    if current_field.key != field_key:
        await callback.answer("Это поле уже неактуально", show_alert=True)
        return

    if fld_type == 'checkbox':
        value = (value_str == 'true')
    elif fld_type == 'select':
        try:
            option_index = int(value_str)
            options = current_field.options_json
            if option_index < 0 or option_index >= len(options):
                raise ValueError
            value = options[option_index]
        except (ValueError, IndexError):
            await callback.answer("Неверный индекс опции", show_alert=True)
            return
    else:
        await callback.answer("Неизвестный тип поля", show_alert=True)
        return

    answers = data.get('answers', {})
    answers[field_key] = value
    await state.update_data(answers=answers, current_field_index=idx+1)

    await callback.message.delete()
    await ask_next_field(callback.message, state)


# --------------------- Обработка аварий ---------------------
async def ask_accident(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Была авария", callback_data="accident_yes"),
         InlineKeyboardButton(text="✅ Не было", callback_data="accident_no")]
    ])
    await message.answer("Была ли аварийная ситуация на этапе?", reply_markup=kb)
    await state.set_state(ReportStates.asking_accident)


async def process_accident(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    stage = data.get('selected_stage')
    if callback.data == "accident_no":
        # Аварии нет – переходим к подтверждению (без аварии)
        await show_summary_and_confirm(callback.message, state, callback.from_user.id, callback.from_user.full_name or "", accident_data=None)
        await callback.message.delete()
        return
    elif callback.data == "accident_yes":
        # Авария есть – спрашиваем тип в зависимости от этапа
        if stage == WellStage.DRILLING:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔧 Устраняется", callback_data="accident_drilling_repairable")],
                [InlineKeyboardButton(text="❌ Неустранимо, закрыть скважину", callback_data="accident_drilling_fatal")]
            ])
            await callback.message.edit_text("Авария при бурении. Что делать?", reply_markup=kb)
            await state.set_state(ReportStates.accident_handling)
        elif stage == WellStage.PUMPING:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔧 Техническая (обнулить время)", callback_data="accident_pumping_technical")],
                [InlineKeyboardButton(text="✅ Результативная (досрочное завершение)", callback_data="accident_pumping_successful")]
            ])
            await callback.message.edit_text("Тип аварии при ОФР:", reply_markup=kb)
            await state.set_state(ReportStates.accident_handling)
        elif stage == WellStage.MODE:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔧 Устраняется", callback_data="accident_mode_repairable")],
                [InlineKeyboardButton(text="❌ Неустранимо, закрыть скважину", callback_data="accident_mode_fatal")]
            ])
            await callback.message.edit_text("Авария на режиме. Что делать?", reply_markup=kb)
            await state.set_state(ReportStates.accident_handling)
        elif stage == WellStage.LIQUIDATION:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Закрыть скважину", callback_data="accident_liquidation_fatal")]
            ])
            await callback.message.edit_text("Авария при ликвидации. Закрыть скважину?", reply_markup=kb)
            await state.set_state(ReportStates.accident_handling)


async def handle_accident_decision(callback: CallbackQuery, state: FSMContext) -> None:
    """Обрабатывает решение по аварии и сразу переходит к подтверждению."""
    data = await state.get_data()
    stage = data.get('selected_stage')
    accident_type = callback.data  # например, accident_drilling_repairable

    # Формируем структуру с информацией об аварии
    accident_data = {'occurred': True, 'type': accident_type.split('_')[2]}  # repairable, fatal, technical, successful

    # Если авария неустранимая – нужно будет потом закрыть скважину
    # Пока просто сохраняем в состоянии
    await state.update_data(accident_data=accident_data)

    # Переходим к подтверждению сводки (с учётом аварии)
    await show_summary_and_confirm(callback.message, state, callback.from_user.id, callback.from_user.full_name or "", accident_data)
    await callback.message.delete()


# --------------------- Подтверждение отправки ---------------------
async def show_summary_and_confirm(message: Message, state: FSMContext, user_id: int, full_name: str, accident_data=None):
    data = await state.get_data()
    well_id = data['selected_well_id']
    answers = data.get('answers', {})
    fields = data.get('form_fields', [])
    stage = data['selected_stage']
    well = await sync_to_async(Well.objects.get)(id=well_id)
    user = await get_or_create_user(user_id, full_name)
    report_date = timezone.localdate()

    text = compose_report_text(report_date, user.full_name or str(user.tg_user_id), well.name, stage, answers, fields, accident_data)

    await state.update_data(preview_text=text, accident_data=accident_data)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_send"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_cancel")]
    ])
    await message.answer(f"Проверьте сводку:\n\n{text}\n\nОтправить?", reply_markup=kb)
    await state.set_state(ReportStates.confirm_send)


async def process_confirm_send(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data == "confirm_send":
        # Сохраняем отчёт (уже отправляет основное сообщение в чат)
        await save_report(callback, state)
        # Переходим к отправке файлов
        await ask_files(callback.message, state)
    else:
        await callback.message.edit_text("Действие отменено.")
        await state.clear()


async def ask_files(message: Message, state: FSMContext):
    """Предлагает отправить файлы к сводке."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово, файлов больше нет", callback_data="files_done")],
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="files_skip")]
    ])
    await message.answer(
        "Теперь вы можете прикрепить файлы (фото, документы) к этой сводке.\n"
        "Просто отправляйте их по одному. Когда закончите, нажмите кнопку.",
        reply_markup=kb
    )
    await state.set_state(ReportStates.sending_files)


async def handle_file(message: Message, state: FSMContext, bot: Bot):
    """Пересылает полученный файл в чат объекта."""
    data = await state.get_data()
    well_id = data.get('selected_well_id')
    if not well_id:
        await message.answer("Ошибка: не найден объект. Начните заново.")
        await state.clear()
        return

    well = await sync_to_async(Well.objects.select_related('object__chat').get)(id=well_id)
    if not well.object.chat:
        await message.answer("У объекта не привязан чат, файл некуда отправить.")
        return

    # Пересылаем файл в чат объекта
    try:
        if message.document:
            await bot.send_document(chat_id=well.object.chat.chat_id, document=message.document.file_id, caption=f"📎 Файл к сводке от {timezone.localdate()}")
        elif message.photo:
            # Берём самое большое фото
            await bot.send_photo(chat_id=well.object.chat.chat_id, photo=message.photo[-1].file_id, caption=f"📎 Фото к сводке от {timezone.localdate()}")
        else:
            await message.answer("Пожалуйста, отправьте документ или фото.")
            return
        await message.answer("✅ Файл отправлен.")
    except Exception as e:
        logger.error(f"Ошибка при отправке файла: {e}")
        await message.answer("❌ Не удалось отправить файл.")


async def process_files_done(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✅ Все файлы отправлены. Сводка полностью готова.")
    await state.clear()


async def process_files_skip(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✅ Отправка завершена.")
    await state.clear()


# --------------------- Сохранение отчёта и логика этапов ---------------------
async def save_report(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    well_id = data['selected_well_id']
    answers = data.get('answers', {})
    fields = data.get('form_fields', [])
    stage = data['selected_stage']
    accident_data = data.get('accident_data')
    preview_text = data.get('preview_text')

    well = await sync_to_async(Well.objects.select_related('object__chat').get)(id=well_id)
    user = await get_or_create_user(callback.from_user.id, callback.from_user.full_name or "")
    report_date = timezone.localdate()

    report = await sync_to_async(Report.objects.create)(
        object=well.object,
        well=well,
        author=user,
        payload_json=answers,
        report_date=report_date,
        message_text=preview_text,
        stage=stage,
    )

    # Отправляем основное сообщение в чат объекта
    if well.object.chat:
        try:
            sent = await callback.bot.send_message(chat_id=well.object.chat.chat_id, text=preview_text)
            report.tg_chat_id = sent.chat.id
            report.tg_message_id = sent.message_id
            await sync_to_async(report.save)(update_fields=['tg_chat_id', 'tg_message_id'])
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение в чат: {e}")
            await callback.message.answer("⚠️ Отчёт сохранён, но не отправлен в чат.")
    else:
        await callback.message.answer("⚠️ У объекта не привязан чат.")

    # Если авария была неустранимой (fatal) – закрываем скважину
    if accident_data and accident_data.get('type') in ('fatal',):
        well.status = WellStatus.CLOSED
        well.closed_at = timezone.now()
        well.closed_reason = 'accident'
        await sync_to_async(well.save)(update_fields=['status', 'closed_at', 'closed_reason'])
        return

    # Обновляем накопленные данные и проверяем завершение этапа
    stage_completed = False

    if stage == WellStage.DRILLING:
        drilled = answers.get('drilled_m')
        if drilled is not None:
            well.current_depth_m = drilled
        pumped = answers.get('pumping_hours_drilling')
        if pumped:
            well.total_drilling_pumping_hours += pumped
        await sync_to_async(well.save)(update_fields=['current_depth_m', 'total_drilling_pumping_hours'])

        if well.planned_depth_m and well.current_depth_m and well.current_depth_m >= well.planned_depth_m:
            stage_completed = True

    # elif stage == WellStage.PUMPING:
    #     pumped = answers.get('pumping_hours')
    #     discharged = answers.get('discharge_hours')
    #     if pumped:
    #         well.total_pumping_hours += pumped
    #     if discharged:
    #         well.total_discharge_hours += discharged
    #     await sync_to_async(well.save)(update_fields=['total_pumping_hours', 'total_discharge_hours'])
    #     if well.planned_pumping_hours and well.total_discharge_hours >= well.planned_pumping_hours:
    #         stage_completed = True
    elif stage == WellStage.PUMPING:
        discharged = answers.get('discharge_hours')
        if discharged:
            well.total_discharge_hours += discharged
        await sync_to_async(well.save)(update_fields=['total_discharge_hours'])
        if well.planned_pumping_hours and well.total_discharge_hours >= well.planned_pumping_hours:
            stage_completed = True

    elif stage == WellStage.MODE:
        if well.remaining_mode_count > 0:
            well.remaining_mode_count -= 1
            await sync_to_async(well.save)(update_fields=['remaining_mode_count'])
            if well.remaining_mode_count == 0:
                stage_completed = True

    elif stage == WellStage.LIQUIDATION:
        if answers.get('liquidation_completed') is True:
            stage_completed = True

    if accident_data and accident_data.get('type') == 'successful':
        stage_completed = True

    if stage_completed:
        next_stage = None
        if stage == WellStage.DRILLING:
            if well.planned_pumping_hours and well.planned_pumping_hours > 0:
                next_stage = WellStage.PUMPING
            elif well.planned_mode_count > 0:
                well.remaining_mode_count = well.planned_mode_count - 1
                await sync_to_async(well.save)(update_fields=['remaining_mode_count'])
                next_stage = WellStage.MODE
            else:
                next_stage = WellStage.LIQUIDATION
        elif stage == WellStage.PUMPING:
            if well.planned_mode_count > 0:
                well.remaining_mode_count = well.planned_mode_count - 1
                await sync_to_async(well.save)(update_fields=['remaining_mode_count'])
                next_stage = WellStage.MODE
            else:
                next_stage = WellStage.LIQUIDATION
        elif stage == WellStage.MODE:
            next_stage = WellStage.LIQUIDATION
        elif stage == WellStage.LIQUIDATION:
            next_stage = WellStage.COMPLETED

        if next_stage:
            await transition_well_stage(well, next_stage, bot=callback.bot)


# --------------------- Обработка решений по закрытию скважин (админы) ---------------------
async def close_request_callback(callback: CallbackQuery) -> None:
    data = callback.data
    if data.startswith("cl_approve_"):
        cr_id = int(data.split("_")[2])
        approve = True
    elif data.startswith("cl_reject_"):
        cr_id = int(data.split("_")[2])
        approve = False
    else:
        return

    cr = await sync_to_async(CloseRequest.objects.select_related(
        'well', 'object', 'initiator', 'report', 'object__chat'
    ).get)(id=cr_id)

    if cr.status != CloseRequestStatus.PENDING:
        await callback.answer("Этот запрос уже обработан", show_alert=True)
        return

    admin = await sync_to_async(TgUser.objects.get)(tg_user_id=callback.from_user.id)

    if approve:
        cr.status = CloseRequestStatus.APPROVED
        cr.decided_by = admin
        cr.decided_at = timezone.now()
        await sync_to_async(cr.save)(update_fields=['status', 'decided_by', 'decided_at'])

        well = cr.well
        well.status = WellStatus.CLOSED
        well.closed_at = timezone.now()
        await sync_to_async(well.save)(update_fields=['status', 'closed_at'])

        remaining = await sync_to_async(Well.objects.filter(object=cr.object, status=WellStatus.ACTIVE).count)()
        if remaining == 0:
            obj = cr.object
            obj.status = ObjectStatus.CLOSED
            await sync_to_async(obj.save)(update_fields=['status'])
            if obj.chat:
                await callback.bot.send_message(
                    chat_id=obj.chat.chat_id,
                    text=f"🏁 Объект «{obj.name}» полностью завершён. Все скважины пробурены."
                )

        await callback.message.edit_text("✅ Запрос подтверждён, скважина закрыта.")
        try:
            await callback.bot.send_message(chat_id=cr.initiator.tg_user_id, text="✅ Ваш запрос на закрытие скважины подтверждён.")
        except:
            pass
    else:
        cr.status = CloseRequestStatus.REJECTED
        cr.decided_by = admin
        cr.decided_at = timezone.now()
        await sync_to_async(cr.save)(update_fields=['status', 'decided_by', 'decided_at'])

        well = cr.well
        if well.status == WellStatus.CLOSING_PENDING:
            well.status = WellStatus.ACTIVE
            await sync_to_async(well.save)(update_fields=['status'])

        await callback.message.edit_text("❌ Запрос отклонён.")
        try:
            await callback.bot.send_message(chat_id=cr.initiator.tg_user_id, text="❌ Ваш запрос на закрытие скважины отклонён.")
        except:
            pass


# --------------------- Хендлер событий изменения членства бота ---------------------
async def on_my_chat_member(update: ChatMemberUpdated) -> None:
    chat = update.chat
    if chat.type not in ('group', 'supergroup'):
        return
    new_status = update.new_chat_member.status
    is_active = new_status in ('member', 'administrator')
    await upsert_known_chat(
        chat_id=chat.id,
        title=chat.title,
        chat_type=chat.type,
        is_active=is_active
    )


async def noop_message(message: Message) -> None:
    return


async def noop_callback(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        pass

async def is_user_in_chat(bot: Bot, user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

class Command(BaseCommand):
    help = 'Запуск Telegram бота (aiogram)'

    def handle(self, *args, **options):
        if not settings.BOT_TOKEN:
            self.stderr.write(self.style.ERROR('BOT_TOKEN не задан в .env'))
            return
        asyncio.run(self.run_bot())

    async def run_bot(self):
        logging.basicConfig(level=logging.INFO)

        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        dp = Dispatcher(storage=MemoryStorage())

        # Глобальные фильтры: только ЛС
        dp.message.filter(F.chat.type == "private")
        dp.callback_query.filter(F.message.chat.type == "private")

        # Регистрация хендлеров
        dp.callback_query.register(process_report_type, ReportStates.choosing_report_type, F.data.in_(['report_type_work', 'report_type_temp']))
        dp.callback_query.register(process_temp_object, ReportStates.choosing_object, F.data.startswith("temp_obj_"))
        dp.message.register(handle_temperature, ReportStates.waiting_temperature)

        dp.message.register(cmd_start, CommandStart())
        dp.message.register(cmd_help, AiogramCommand('help'))
        dp.message.register(cmd_cancel, AiogramCommand('cancel'))

        dp.message.register(cmd_create_object, AiogramCommand('create_object'))
        dp.message.register(process_object_name, CreateObjectStates.waiting_for_name)

        dp.callback_query.register(process_chat_choice, CreateObjectStates.waiting_for_chat, F.data.startswith(('chat_', 'skip_chat')))
        dp.message.register(finish_wells, CreateObjectStates.waiting_for_wells, AiogramCommand('done'))
        dp.message.register(process_well, CreateObjectStates.waiting_for_wells)

        dp.message.register(cmd_report, AiogramCommand('report'))
        dp.callback_query.register(process_report_object, ReportStates.choosing_object, F.data.startswith("rep_obj_"))
        dp.callback_query.register(process_report_stage, ReportStates.choosing_stage, F.data.startswith("rep_stage_"))
        dp.callback_query.register(process_report_well, ReportStates.choosing_well, F.data.startswith("rep_well_"))

        dp.message.register(handle_field_text, ReportStates.entering_fields)
        dp.callback_query.register(handle_field_callback, ReportStates.entering_fields, F.data.startswith("fld:"))

        # Аварии и подтверждение
        dp.callback_query.register(process_accident, ReportStates.asking_accident, F.data.in_(['accident_yes', 'accident_no']))
        dp.callback_query.register(handle_accident_decision, ReportStates.accident_handling, F.data.startswith(('accident_drilling_', 'accident_pumping_', 'accident_mode_', 'accident_liquidation_')))
        dp.callback_query.register(process_confirm_send, ReportStates.confirm_send, F.data.in_(['confirm_send', 'confirm_cancel']))

        # Обработка файлов после отправки сводки
        dp.message.register(handle_file, ReportStates.sending_files, F.content_type.in_(['document', 'photo']))
        dp.callback_query.register(process_files_done, ReportStates.sending_files, F.data == "files_done")
        dp.callback_query.register(process_files_skip, ReportStates.sending_files, F.data == "files_skip")

        # Запросы закрытия (если нужны)
        dp.callback_query.register(close_request_callback, F.data.startswith("cl_"))

        # Изменение членства
        dp.my_chat_member.register(on_my_chat_member)

        # Глушилки
        dp.message.register(noop_message, ~F.chat.type.in_({"private"}))
        dp.callback_query.register(noop_callback, F.message.chat.type != "private")

        self.stdout.write(self.style.SUCCESS('Бот запущен и готов к работе'))

        try:
            await dp.start_polling(
                bot,
                allowed_updates=['message', 'callback_query', 'my_chat_member']
            )
        finally:
            await bot.session.close()