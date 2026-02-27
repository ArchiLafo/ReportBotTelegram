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
from aiogram.types import Message, ChatMemberUpdated, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from core.models import (
    TgUser, TgRole, KnownChat, KnownChatType,
    OrgObject, Well, WellStatus, ObjectStatus, ObjectType,
    Form, FormField, Report, CloseRequest, CloseRequestStatus,
    WellStage
)

logger = logging.getLogger(__name__)


# --------------------- Состояния FSM ---------------------
class CreateObjectStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_type = State()
    waiting_for_chat = State()
    waiting_for_wells = State()


class ReportStates(StatesGroup):
    choosing_report_type = State()
    choosing_object = State()
    choosing_stage = State()
    choosing_well = State()
    entering_fields = State()
    asking_force_majeure = State()
    waiting_temperature = State() 


class UnfreezeStates(StatesGroup):
    choosing_well = State()


# --------------------- Вспомогательные функции для работы с БД ---------------------
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


@sync_to_async
def get_active_objects():
    from django.db.models import Exists, OuterRef
    active_wells = Well.objects.filter(object=OuterRef('pk'), status=WellStatus.ACTIVE).exclude(stage=WellStage.FORCE_MAJEURE)
    return list(OrgObject.objects.filter(status=ObjectStatus.ACTIVE).annotate(
        has_active_wells=Exists(active_wells)
    ).filter(has_active_wells=True).order_by('name'))


@sync_to_async
def get_stages_with_wells(object_id):
    """Возвращает уникальные этапы, на которых есть активные (не замороженные) скважины для данного объекта."""
    stages = Well.objects.filter(
        object_id=object_id,
        status=WellStatus.ACTIVE
    ).exclude(stage=WellStage.FORCE_MAJEURE).values_list('stage', flat=True)
    # Принудительно убираем дубликаты через set
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


def compose_report_text(report_date, author_name, well_name, answers, fields, force_majeure=False):
    lines = []
    lines.append(f"{report_date.strftime('%d.%m.%Y')} — {author_name}")
    lines.append(f"Скважина: {well_name}")
    if force_majeure:
        lines.append("⚠️ ФОРС-МАЖОР")
    else:
        lines.append("✅ Сегодня без форс-мажоров")
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
    elif new_stage == WellStage.MAINTENANCE:
        well.maintenance_started_at = now
    elif new_stage == WellStage.LIQUIDATION:
        pass
    elif new_stage == WellStage.COMPLETED:
        well.closed_at = now
        well.status = WellStatus.CLOSED

    await sync_to_async(well.save)(update_fields=['stage', 'pumping_started_at', 'maintenance_started_at', 'closed_at', 'status'])

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
        "/unfreeze - разморозить скважину (админ/разработчик)\n"
        "(другие команды в разработке)"
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

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Частный", callback_data="private"),
         InlineKeyboardButton(text="Федеральный", callback_data="federal")]
    ])
    await message.answer("Выберите тип объекта:", reply_markup=kb)
    await state.set_state(CreateObjectStates.waiting_for_type)


async def process_object_type(callback: CallbackQuery, state: FSMContext) -> None:
    obj_type = callback.data
    await state.update_data(object_type=obj_type)

    await callback.message.edit_text(f"Тип объекта: {'Частный' if obj_type=='private' else 'Федеральный'}")

    chats = await sync_to_async(list)(
        KnownChat.objects.filter(is_active=True, bound_object__isnull=True)
    )
    if not chats:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить (привязать позже)", callback_data="skip_chat")]
        ])
        await callback.message.answer(
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
    await callback.message.answer("Выберите чат для привязки объекта или пропустите:", reply_markup=kb)
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
        "<b>Название Глубина [Часы_ОФР] [Сопровождение(да/нет)]</b>\n"
        "Пример: Скв-1 150 24 да\n"
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
    maintenance_needed = False
    if len(parts) >= 3:
        try:
            pumping_hours = float(parts[2].replace(',', '.'))
        except ValueError:
            await message.answer("Часы ОФР должны быть числом.")
            return
    if len(parts) >= 4:
        maintenance_needed = parts[3].lower() in ['да', 'yes', '1']

    data = await state.get_data()
    wells = data.get('wells', [])
    wells.append({
        'name': name,
        'depth': depth,
        'pumping_hours': pumping_hours,
        'maintenance_needed': maintenance_needed
    })
    await state.update_data(wells=wells)

    await message.answer(
        f"Скважина {name} добавлена.\n"
        "Можете добавить ещё или введите /done для завершения."
    )


async def finish_wells(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    object_name = data.get('object_name')
    object_type = data.get('object_type', ObjectType.PRIVATE)
    chat_id = data.get('chat_id')
    wells_data = data.get('wells', [])

    obj = await sync_to_async(OrgObject.objects.create)(
        name=object_name,
        object_type=object_type,
        status=ObjectStatus.ACTIVE
    )

    if chat_id:
        # Проверяем, не привязан ли уже этот чат к другому объекту
        existing_obj = await sync_to_async(OrgObject.objects.filter(chat_id=chat_id).first)()
        if existing_obj:
            await message.answer("⚠️ Внимание: выбранный чат уже привязан к другому объекту. Объект создан без привязки к чату.")
        else:
            # Привязываем чат
            chat = await sync_to_async(KnownChat.objects.get)(chat_id=chat_id)
            obj.chat = chat
            await sync_to_async(obj.save)(update_fields=['chat'])

    for w in wells_data:
        await sync_to_async(Well.objects.create)(
            object=obj,
            name=w['name'],
            planned_depth_m=w['depth'],
            planned_pumping_hours=w['pumping_hours'],
            maintenance_needed=w['maintenance_needed'],
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
        # Переходим к выбору объекта (существующая логика)
        objects = await get_active_objects()
        if not objects:
            await callback.message.edit_text("Нет доступных объектов для отчёта.")
            await state.clear()
            return
        await state.update_data(objects_list=objects)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=obj.name, callback_data=f"rep_obj_{obj.id}")]
            for obj in objects
        ])
        await callback.message.edit_text("Выберите объект:", reply_markup=kb)
        await state.set_state(ReportStates.choosing_object)
    elif callback.data == "report_type_temp":
        # Для температуры сначала выбираем объект
        objects = await get_active_objects()
        if not objects:
            await callback.message.edit_text("Нет доступных объектов для отчёта.")
            await state.clear()
            return
        await state.update_data(objects_list=objects)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=obj.name, callback_data=f"temp_obj_{obj.id}")]
            for obj in objects
        ])
        await callback.message.edit_text("Выберите объект для температурной сводки:", reply_markup=kb)
        await state.set_state(ReportStates.choosing_object)  # используем то же состояние, но данные отметим позже

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
    # Строим кнопки этапов
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

    # Формируем текст
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
        stage=None,  # для температуры этап не указываем
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
    stage = data['selected_stage']          # получаем этап из данных

    # Сохраняем текущую глубину, если этап бурения
    if stage == WellStage.DRILLING:
        well = await sync_to_async(Well.objects.get)(id=well_id)
        await state.update_data(current_depth=well.current_depth_m)

    form_code_map = {
        WellStage.DRILLING: 'drilling_daily',
        WellStage.PUMPING: 'pumping_daily',
        WellStage.MAINTENANCE: 'maintenance_daily',
        WellStage.LIQUIDATION: 'liquidation_daily',
    }
    form_code = form_code_map.get(stage)
    if not form_code:
        await callback.message.edit_text("Для данного этапа нет формы отчёта.")
        await state.clear()
        return

    fields = await get_form_fields(form_code)
    if not fields:
        # Если полей нет, сразу переходим к вопросу о форс-мажоре
        await ask_force_majeure(callback.message, state)
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
        # Все поля заполнены, переходим к вопросу о форс-мажоре
        await ask_force_majeure(message, state)
        return
    field = fields[idx]
    text = f"<b>{field.label}</b>" + (" (обязательное)" if field.required else "")
    # Если это поле "drilled_m", показываем текущую глубину
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


async def ask_force_majeure(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Был форс-мажор", callback_data="fm_yes"),
         InlineKeyboardButton(text="✅ Не было", callback_data="fm_no")]
    ])
    await message.answer("Был ли форс-мажор на этапе?", reply_markup=kb)
    await state.set_state(ReportStates.asking_force_majeure)


async def process_force_majeure(callback: CallbackQuery, state: FSMContext) -> None:
    fm = (callback.data == "fm_yes")
    data = await state.get_data()
    well_id = data['selected_well_id']
    answers = data.get('answers', {})
    fields = data.get('form_fields', [])
    stage = data.get('selected_stage')
    well = await sync_to_async(Well.objects.select_related('object__chat').get)(id=well_id)
    user = await sync_to_async(TgUser.objects.get)(tg_user_id=callback.from_user.id)
    report_date = timezone.localdate()

    if fm:
        # Запоминаем предыдущий этап
        well.previous_stage = well.stage
        well.stage = WellStage.FORCE_MAJEURE
        await sync_to_async(well.save)(update_fields=['stage', 'previous_stage'])

    text = compose_report_text(report_date, user.full_name or str(user.tg_user_id), well.name, answers, fields, force_majeure=fm)

    report = await sync_to_async(Report.objects.create)(
        object=well.object,
        well=well,
        author=user,
        payload_json=answers,
        report_date=report_date,
        message_text=text,
        stage=stage
    )

    if well.object.chat:
        try:
            sent = await callback.bot.send_message(chat_id=well.object.chat.chat_id, text=text)
            report.tg_chat_id = sent.chat.id
            report.tg_message_id = sent.message_id
            await sync_to_async(report.save)(update_fields=['tg_chat_id', 'tg_message_id'])
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение в чат: {e}")
            await callback.message.answer("⚠️ Отчёт сохранён, но не отправлен в чат.")
    else:
        await callback.message.answer("⚠️ У объекта не привязан чат.")

    if fm:
        await callback.message.edit_text("✅ Отчёт отправлен. Скважина заморожена из-за форс-мажора.")
        await state.clear()
        return

    # Если не форс-мажор, проверяем завершение этапа
    stage_completed = False

    if stage == WellStage.DRILLING:
        drilled = answers.get('drilled_m')
        if drilled is not None:
            well.current_depth_m = drilled
            await sync_to_async(well.save)(update_fields=['current_depth_m'])
            if well.planned_depth_m and drilled >= well.planned_depth_m:
                stage_completed = True
    elif stage == WellStage.PUMPING:
        pumped = answers.get('pumping_hours')
        if pumped:
            well.total_pumping_hours += pumped
            await sync_to_async(well.save)(update_fields=['total_pumping_hours'])
            if well.planned_pumping_hours and well.total_pumping_hours >= well.planned_pumping_hours:
                stage_completed = True
    elif stage == WellStage.MAINTENANCE:
        if answers.get('maintenance_completed') is True:
            stage_completed = True
    elif stage == WellStage.LIQUIDATION:
        if answers.get('liquidation_completed') is True:
            stage_completed = True

    if stage_completed:
        # Определяем следующий этап в зависимости от текущего и флага сопровождения
        if stage == WellStage.DRILLING:
            next_stage = WellStage.PUMPING
        elif stage == WellStage.PUMPING:
            # После ОФР: если требуется сопровождение, идём на него, иначе сразу на завершено
            next_stage = WellStage.MAINTENANCE if well.maintenance_needed else WellStage.COMPLETED
        elif stage == WellStage.MAINTENANCE:
            next_stage = WellStage.LIQUIDATION
        elif stage == WellStage.LIQUIDATION:
            next_stage = WellStage.COMPLETED
        else:
            next_stage = None

        if next_stage:
            await transition_well_stage(well, next_stage, bot=callback.bot)
            await callback.message.edit_text(f"✅ Отчёт отправлен. Этап завершён, скважина переведена на этап {WellStage(next_stage).label}.")
        else:
            await callback.message.edit_text("✅ Отчёт отправлен. Этап завершён.")
    else:
        await callback.message.edit_text("✅ Отчёт отправлен.")

    await state.clear()


# --------------------- Разморозка скважины (админ) ---------------------
async def cmd_unfreeze(message: Message, state: FSMContext) -> None:
    if not await check_user_role(message, [TgRole.ADMIN, TgRole.DEVELOPER]):
        return
    frozen_wells = await sync_to_async(list)(Well.objects.filter(
        stage=WellStage.FORCE_MAJEURE,
        status=WellStatus.ACTIVE
    ).select_related('object'))
    if not frozen_wells:
        await message.answer("Нет замороженных скважин.")
        return
    await state.update_data(frozen_wells=frozen_wells)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{w.object.name} / {w.name}", callback_data=f"unfreeze_{w.id}")]
        for w in frozen_wells
    ])
    await message.answer("Выберите скважину для разморозки:", reply_markup=kb)
    await state.set_state(UnfreezeStates.choosing_well)


async def process_unfreeze_well(callback: CallbackQuery, state: FSMContext) -> None:
    well_id = int(callback.data.split("_")[1])
    well = await sync_to_async(Well.objects.get)(id=well_id)
    if well.stage != WellStage.FORCE_MAJEURE:
        await callback.message.edit_text("Эта скважина уже не заморожена.")
        await state.clear()
        return
    # Возвращаем предыдущий этап
    previous = well.previous_stage or WellStage.DRILLING
    well.stage = previous
    well.previous_stage = None
    await sync_to_async(well.save)(update_fields=['stage', 'previous_stage'])
    await callback.message.edit_text(f"✅ Скважина {well.name} разморожена и возвращена на этап {WellStage(previous).label}.")
    await state.clear()


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
    """Глушим любые сообщения не из ЛС, чтобы не было 'Update ... is not handled'."""
    return


async def noop_callback(callback: CallbackQuery) -> None:
    """Глушим любые callback не из ЛС, чтобы не было 'Update ... is not handled'."""
    try:
        await callback.answer()
    except Exception:
        pass


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

        # ------------------------------------------------------------
        # 1) Глобальные фильтры: ВСЁ общение с ботом - только в ЛС
        # ------------------------------------------------------------
        dp.message.filter(F.chat.type == "private")
        dp.callback_query.filter(F.message.chat.type == "private")

        # ------------------------------------------------------------
        # 2) Регистрируем хендлеры ЛС (без повторения F.chat.type)
        # ------------------------------------------------------------
         # Выбор типа отчёта
        dp.callback_query.register(process_report_type, ReportStates.choosing_report_type, F.data.in_(['report_type_work', 'report_type_temp']))

        # Обработка выбора объекта для температуры
        dp.callback_query.register(process_temp_object, ReportStates.choosing_object, F.data.startswith("temp_obj_"))

        # Ввод температуры
        dp.message.register(handle_temperature, ReportStates.waiting_temperature)

        # Общие команды (ЛС)
        dp.message.register(cmd_start, CommandStart())
        dp.message.register(cmd_help, AiogramCommand('help'))
        dp.message.register(cmd_cancel, AiogramCommand('cancel'))

        # Создание объекта (админ) — ЛС
        dp.message.register(cmd_create_object, AiogramCommand('create_object'))
        dp.message.register(process_object_name, CreateObjectStates.waiting_for_name)

        dp.callback_query.register(
            process_object_type,
            CreateObjectStates.waiting_for_type,
            F.data.in_(['private', 'federal'])
        )

        dp.callback_query.register(
            process_chat_choice,
            CreateObjectStates.waiting_for_chat,
            F.data.startswith(('chat_', 'skip_chat'))
        )

        dp.message.register(
            finish_wells,
            CreateObjectStates.waiting_for_wells,
            AiogramCommand('done')
        )
        dp.message.register(process_well, CreateObjectStates.waiting_for_wells)

        # Отчёт — ЛС
        dp.message.register(cmd_report, AiogramCommand('report'))

        dp.callback_query.register(
            process_report_object,
            ReportStates.choosing_object,
            F.data.startswith("rep_obj_")
        )
        dp.callback_query.register(
            process_report_stage,
            ReportStates.choosing_stage,
            F.data.startswith("rep_stage_")
        )
        dp.callback_query.register(
            process_report_well,
            ReportStates.choosing_well,
            F.data.startswith("rep_well_")
        )

        dp.message.register(handle_field_text, ReportStates.entering_fields)

        dp.callback_query.register(
            handle_field_callback,
            ReportStates.entering_fields,
            F.data.startswith("fld:")
        )

        dp.callback_query.register(
            process_force_majeure,
            ReportStates.asking_force_majeure,
            F.data.in_(['fm_yes', 'fm_no'])
        )

        # Разморозка — ЛС
        dp.message.register(cmd_unfreeze, AiogramCommand('unfreeze'))

        dp.callback_query.register(
            process_unfreeze_well,
            UnfreezeStates.choosing_well,
            F.data.startswith("unfreeze_")
        )

        # Запросы закрытия (уведомления админам приходят в ЛС) — ЛС
        dp.callback_query.register(
            close_request_callback,
            F.data.startswith("cl_")
        )

        # ------------------------------------------------------------
        # 3) Групповые события/апдейты
        # ------------------------------------------------------------

        # Изменение членства бота (в группах) — НЕ попадает под dp.message.filter
        dp.my_chat_member.register(on_my_chat_member)

        # ------------------------------------------------------------
        # 4) "Глушилки" для НЕ-private апдейтов
        #    Чтобы не было "Update ... is not handled"
        # ------------------------------------------------------------
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