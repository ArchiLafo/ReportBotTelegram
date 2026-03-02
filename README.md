Техническая документация проекта Report Bot
1. Введение
Report Bot — это Telegram-бот для автоматизации сбора ежедневных отчётов по объектам работ.
Основные задачи:

Позволить сотрудникам (заполнителям) быстро отправлять сводки о ходе работ.

Автоматически публиковать сводки в специальных чатах, привязанных к объектам.

Отслеживать жизненный цикл скважин: бурение → ОФР → режим → ликвидация.

Обрабатывать аварийные ситуации и корректно переводить скважины между этапами.

Дать администраторам возможность создавать объекты и скважины прямо через бота, без необходимости заходить в админку Django.

Здесь подробно разобрана архитектура, модели данных, ключевые участки кода и обоснование принятых решений.

2. Общая архитектура
Проект построен на двух основных компонентах, работающих в связке:

Django (версия 5) – веб-фреймворк, обеспечивающий:

Модели данных и ORM (Object-Relational Mapping).

Административную панель для удобного просмотра и редактирования данных.

Миграции схемы базы данных.

Возможность в будущем легко добавить REST API или веб-интерфейс.

aiogram (версия 3) – асинхронная библиотека для работы с Telegram Bot API. Выбрана из-за высокой производительности, удобной системы FSM (Finite State Machine) и хорошей документации.

Взаимодействие: бот работает в режиме long polling (постоянно опрашивает Telegram о новых событиях). Все данные хранятся в базе данных SQLite. Поскольку Django ORM синхронный, а aiogram асинхронный, используется специальная обёртка sync_to_async из пакета asgiref, которая позволяет вызывать синхронные методы ORM из асинхронного кода без блокировки.

Структура проекта (упрощённо):

report_bot/
├── core/                    # приложение Django с моделями
│   ├── models.py            # все модели данных
│   └── admin.py             # регистрация моделей в админке
├── bot/                      # приложение с логикой бота
│   └── management/
│       └── commands/
│           └── runbot.py     # единственный файл с кодом бота
├── orgbot/                   # настройки Django
├── manage.py
├── .env                      # переменные окружения (токен и т.п.)
└── db.sqlite3
3. Модели данных (core/models.py)
Модели спроектированы так, чтобы максимально отражать предметную область и обеспечивать гибкость (например, динамические формы). Рассмотрим каждую модель подробно.

3.1. Пользователи и чаты
TgUser – модель пользователя Telegram, который взаимодействует с ботом.

python
class TgUser(models.Model):
    tg_user_id = models.BigIntegerField(unique=True)
    full_name = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=20, choices=TgRole.choices, default=TgRole.FILLER)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
tg_user_id – уникальный идентификатор пользователя в Telegram, используется для всех обращений к БД.

role – роль: developer, admin, filler. Роли определяют доступ к командам (например, создание объектов доступно только админам и разработчикам).

is_active – позволяет временно заблокировать пользователя без удаления из БД.

full_name – кешируется для отображения в сводках (имя может меняться, но мы храним последнее известное).

KnownChat – чаты (группы/супергруппы), в которых присутствует бот.

python
class KnownChat(models.Model):
    chat_id = models.BigIntegerField(unique=True)
    title = models.CharField(max_length=255, blank=True)
    chat_type = models.CharField(max_length=20, choices=KnownChatType.choices)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
Бот автоматически добавляет/обновляет запись при добавлении в группу (через хендлер on_my_chat_member).

Поле is_active указывает, присутствует ли бот в чате в данный момент (может стать False, если бота удалили).

Эти чаты используются для привязки к объектам: каждая группа может быть привязана только к одному объекту (связь OneToOneField).

3.2. Объекты и скважины
OrgObject – объект работ (например, «Горячегорск»).

python
class OrgObject(models.Model):
    name = models.CharField(max_length=255)
    passport_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=ObjectStatus.choices, default=ObjectStatus.ACTIVE)
    chat = models.OneToOneField(KnownChat, null=True, blank=True, on_delete=models.SET_NULL, related_name='bound_object')
    created_at = models.DateTimeField(default=timezone.now)
name – название объекта, отображается в списках.

passport_json – зарезервировано для хранения произвольных паспортных данных объекта в будущем.

status – активен или закрыт (закрывается автоматически, когда все скважины завершены).

chat – привязка к чату Telegram. Установлена OneToOneField, что гарантирует уникальность привязки (один чат – один объект).

WellStage и WellStatus – перечисления для состояний скважины.

python
class WellStage(models.TextChoices):
    DRILLING = 'drilling', 'Бурение'
    PUMPING = 'pumping', 'ОФР'
    MODE = 'mode', 'Режим'
    LIQUIDATION = 'liquidation', 'Ликвидация'
    COMPLETED = 'completed', 'Завершена'

class WellStatus(models.TextChoices):
    ACTIVE = 'active', 'Активна'
    CLOSED = 'closed', 'Закрыта'
    CLOSING_PENDING = 'closing_pending', 'Ожидает подтверждения закрытия'  # устаревшее, оставлено для совместимости
WellStage отражает этап жизненного цикла скважины. Последовательность строгая: бурение → ОФР → режим → ликвидация → завершена.

WellStatus – активна или закрыта (может быть закрыта по завершении или из-за неустранимой аварии).

Well – основная модель, содержащая все параметры скважины.

python
class Well(models.Model):
    object = models.ForeignKey(OrgObject, on_delete=models.CASCADE, related_name='wells')
    name = models.CharField(max_length=100)
    planned_depth_m = models.FloatField(null=True, blank=True)
    current_depth_m = models.FloatField(null=True, blank=True)
    stage = models.CharField(max_length=20, choices=WellStage.choices, default=WellStage.DRILLING)
    planned_pumping_hours = models.FloatField(null=True, blank=True)
    pumping_started_at = models.DateTimeField(null=True, blank=True)
    pumping_completed_at = models.DateTimeField(null=True, blank=True)
    mode_started_at = models.DateTimeField(null=True, blank=True)
    mode_completed_at = models.DateTimeField(null=True, blank=True)
    liquidation_completed_at = models.DateTimeField(null=True, blank=True)
    total_pumping_hours = models.FloatField(default=0.0)        # накоплено часов прокачки при ОФР
    total_discharge_hours = models.FloatField(default=0.0)      # накоплено часов откачки при ОФР
    total_drilling_pumping_hours = models.FloatField(default=0.0) # часы прокачки при бурении
    planned_mode_count = models.IntegerField(default=0)          # сколько раз нужно провести режим
    remaining_mode_count = models.IntegerField(default=0)        # сколько осталось
    closed_reason = models.CharField(max_length=50, blank=True, null=True)
    status = models.CharField(max_length=30, choices=WellStatus.choices, default=WellStatus.ACTIVE)
    closed_at = models.DateTimeField(null=True, blank=True)
Поля для бурения: planned_depth_m, current_depth_m, total_drilling_pumping_hours.

Поля для ОФР: planned_pumping_hours, total_pumping_hours, total_discharge_hours, даты начала/завершения.

Поля для режима: planned_mode_count, remaining_mode_count, даты.

Поля для ликвидации: liquidation_completed_at (дата завершения).

Общие поля: stage, status, closed_reason (причина закрытия, например 'accident').

Почему хранятся и плановые, и накопленные значения?
Накопленные значения нужны для проверки завершения этапа: например, ОФР завершается, когда total_discharge_hours достигнет planned_pumping_hours. Плановые значения задаются при создании скважины и могут быть изменены администратором позже.

3.3. Динамические формы
Form – представляет собой шаблон формы для определённого этапа.

python
class Form(models.Model):
    code = models.CharField(max_length=100, unique=True)
    title = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
code – уникальный идентификатор, используется в коде для выбора формы (например, 'drilling_daily').

title – человеко-читаемое название.

is_active – позволяет временно отключить форму без удаления.

FormField – поле внутри формы.

python
class FormField(models.Model):
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='fields')
    key = models.CharField(max_length=100)           # латиница, без пробелов
    label = models.CharField(max_length=255)         # отображаемое название
    type = models.CharField(max_length=20, choices=FieldType.choices)
    required = models.BooleanField(default=False)
    options_json = models.JSONField(default=list, blank=True)  # для типа 'select'
    order_index = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
key – используется как ключ в словаре ответов (answers).

type – поддерживаются text, number, checkbox, select.

options_json – массив строк для выпадающего списка.

order_index – порядок отображения полей.

Почему формы динамические?
Это позволяет администратору изменять набор полей в отчётах без изменения кода бота. Достаточно отредактировать записи в БД через админку Django. Например, можно добавить поле «Температура раствора» в форму бурения, и бот автоматически начнёт его запрашивать.

3.4. Отчёты
Report – модель для хранения отправленных сводок.

python
class Report(models.Model):
    object = models.ForeignKey(OrgObject, on_delete=models.PROTECT)
    well = models.ForeignKey(Well, on_delete=models.PROTECT, null=True, blank=True)
    author = models.ForeignKey(TgUser, on_delete=models.PROTECT)
    report_date = models.DateField(default=timezone.localdate)
    stage = models.CharField(max_length=20, choices=WellStage.choices, null=True, blank=True)
    payload_json = models.JSONField(default=dict, blank=True)
    message_text = models.TextField(blank=True)
    tg_chat_id = models.BigIntegerField(null=True, blank=True)
    tg_message_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
payload_json – хранит ответы пользователя в виде словаря {key: value}.

message_text – итоговый текст, который был отправлен в чат (формируется функцией compose_report_text).

tg_chat_id и tg_message_id – позволяют в будущем редактировать сообщение (пока не используется).

stage – дублирует этап на момент отчёта (для удобства выборки).

Почему PROTECT вместо CASCADE?
Отчёты – важные архивные данные, их нельзя удалять автоматически при удалении объекта или скважины. Django защитит от удаления, пока есть связанные отчёты.

3.5. Запросы на закрытие (устаревший механизм)
CloseRequest – модель для запросов на закрытие скважины с подтверждением администратора. Этот функционал был заменён автоматическим завершением этапов и авариями, но код оставлен для обратной совместимости. В текущей версии не используется.

4. Организация кода бота (bot/management/commands/runbot.py)
Весь код бота находится в одном файле. Это сознательное решение для упрощения разработки и отладки на этапе MVP. В будущем, при росте проекта, его можно разбить на модули.

4.1. Импорты и настройки
В начале файла импортируются все необходимые модули, включая модели и aiogram. Также настраивается логгер.

4.2. Состояния FSM
Классы-состояния определяют шаги диалога:

python
class CreateObjectStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_chat = State()
    waiting_for_wells = State()

'''class ReportStates(StatesGroup):
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
Каждое состояние соответствует определённому этапу взаимодействия с пользователем. Переходы между состояниями управляются хендлерами.

4.3. Вспомогательные функции (с префиксом @sync_to_async)
Функции, работающие с базой данных, обёрнуты в @sync_to_async, чтобы их можно было вызывать из асинхронного кода. Пример:

python
@sync_to_async
def get_or_create_user(tg_user_id: int, full_name: str) -> TgUser:
    user, created = TgUser.objects.get_or_create(...)
    return user
Такие функции используются для получения данных перед отправкой сообщений или после получения ответов.

4.4. Хендлеры
Каждый хендлер – асинхронная функция, принимающая объект события (Message, CallbackQuery) и контекст FSM. Они регистрируются в диспетчере с указанием фильтров (команда, состояние, тип данных и т.д.).

Пример простого хендлера команды:

python
async def cmd_start(message: Message) -> None:
    user = await get_or_create_user(...)
    await message.answer(f"Привет, {user.full_name}!")
Пример хендлера callback-запроса с состоянием:

python
@dp.callback_query(ReportStates.choosing_object, F.data.startswith("rep_obj_"))
async def process_report_object(callback: CallbackQuery, state: FSMContext):
    obj_id = int(callback.data.split("_")[2])
    stages = await get_stages_with_wells(obj_id)
    # ... формирование клавиатуры
    await state.set_state(ReportStates.choosing_stage)
Почему хендлеры такие длинные?
Потому что в них сосредоточена вся бизнес-логика: проверка прав, загрузка данных из БД, формирование ответа, переход в следующее состояние. Разбиение на более мелкие функции было бы возможным, но на данном этапе это увеличило бы сложность навигации.

4.5. Ключевые функции
compose_report_text
Формирует текст сводки на основе даты, автора, названия скважины, ответов и информации об аварии. Добавляет соответствующую строку (⚠️ Авария... или ✅ Без аварий).

transition_well_stage
Переводит скважину на новый этап, обновляет соответствующие поля (даты начала) и отправляет уведомление в чат объекта.

save_report
Самая объёмная функция. Выполняет:

Создание записи Report.

Отправку сообщения в чат объекта.

Обработку аварий (если тип fatal – закрытие скважины).

Обновление накопленных данных скважины (глубина, часы, остаток режимов).

Проверку завершения текущего этапа и, если нужно, вызов transition_well_stage для перехода на следующий.

handle_accident_decision
Обрабатывает выбор типа аварии, сохраняет информацию в состоянии и переходит к подтверждению.

handle_file
Пересылает полученный файл в чат объекта, используя метод send_document или send_photo.

4.6. Регистрация хендлеров в run_bot
Все хендлеры регистрируются в методе run_bot после создания диспетчера:

python
dp.message.register(cmd_start, CommandStart())
dp.callback_query.register(process_report_type, ReportStates.choosing_report_type, F.data.in_(['report_type_work', 'report_type_temp']))
...
dp.message.register(handle_file, ReportStates.sending_files, F.content_type.in_(['document', 'photo']))
Глобальные фильтры устанавливаются сразу после создания диспетчера:

python
dp.message.filter(F.chat.type == "private")
dp.callback_query.filter(F.message.chat.type == "private")
Это гарантирует, что бот реагирует только на сообщения из личных чатов. Сообщения из групп игнорируются, кроме события my_chat_member (добавление/удаление бота).

5. Основные сценарии работы (подробно)
5.1. Создание объекта (администратор)
Последовательность шагов:

Пользователь вводит /create_object.

Хендлер cmd_create_object проверяет роль (должен быть ADMIN или DEVELOPER) и переводит состояние в CreateObjectStates.waiting_for_name.

process_object_name получает название, сохраняет его в FSM и загружает список свободных чатов.

Если чатов нет – предлагает пропустить.

Если есть – показывает инлайн-кнопки с названиями чатов и кнопку «Пропустить».

process_chat_choice обрабатывает выбор: сохраняет chat_id или None, переводит в waiting_for_wells и просит ввести данные скважины.

process_well парсит строку (формат: Название Глубина [Часы_ОФР] [Количество_режимов]), сохраняет в список в состоянии.

При вводе /done срабатывает finish_wells: создаётся объект, скважины, привязывается чат (если выбран), отправляется сообщение об успехе, состояние очищается.

Почему ввод скважин одной строкой, а не пошагово?
Чтобы ускорить процесс для администратора, который часто создаёт много скважин. Можно было бы сделать пошаговый ввод, но это заняло бы больше времени.

5.2. Создание рабочей сводки
/report → cmd_report показывает две кнопки: «Работа» и «Температура».

При выборе «Работа» (process_report_type) загружаются активные объекты, показывается список.

process_report_object – выбирается объект, затем для него определяются этапы с активными скважинами, показываются кнопки этапов.

process_report_stage – выбирается этап, загружаются скважины этого этапа, показываются кнопки скважин.

process_report_well – выбирается скважина. Определяется код формы по этапу, загружаются поля формы. Запускается ask_next_field.

ask_next_field последовательно задаёт вопросы. Для числовых полей проверяется корректность ввода. Для drilled_m дополнительно показывается текущая глубина (если есть).

После ввода всех полей вызывается ask_accident – вопрос о наличии аварии.

process_accident:

Если аварии нет – сразу show_summary_and_confirm.

Если есть – в зависимости от этапа показываются варианты аварий, состояние переходит в accident_handling.

handle_accident_decision сохраняет тип аварии в состоянии и вызывает show_summary_and_confirm (с учётом аварии).

show_summary_and_confirm формирует предварительный текст сводки (функция compose_report_text), показывает его с кнопками «Отправить» / «Отмена», переводит в confirm_send.

process_confirm_send при подтверждении вызывает save_report, затем ask_files.

ask_files предлагает отправить файлы, переводит в sending_files.

Пользователь отправляет файлы – handle_file пересылает их в чат объекта, подтверждая получение.

По кнопке «Готово» (process_files_done) или «Пропустить» (process_files_skip) состояние очищается, диалог завершается.

5.3. Температурная сводка
При выборе «Температура» process_report_type показывает список объектов (с префиксом temp_obj_).

process_temp_object сохраняет выбранный объект и переводит в waiting_temperature, просит ввести температуру.

handle_temperature создаёт отчёт, отправляет сообщение в чат объекта и завершает диалог.

5.4. Обработка аварий в save_report
Внутри save_report после создания отчёта и отправки проверяется, была ли авария:

python
if accident_data and accident_data.get('type') in ('fatal',):
    well.status = WellStatus.CLOSED
    well.closed_at = timezone.now()
    well.closed_reason = 'accident'
    await sync_to_async(well.save)(...)
    return  # не проверяем завершение этапа
Если авария неустранимая – скважина закрывается, этап не завершается (переход не происходит).

Для других типов аварий они учитываются при проверке завершения этапа:

Техническая (technical) – обнуление часов, этап не завершается (специально не меняем stage_completed).

Результативная (successful) – принудительно устанавливаем stage_completed = True.

5.5. Автоматический переход этапов
Логика переходов сосредоточена в конце save_report:

python
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
Почему для режима при переходе с бурения и ОФР устанавливается remaining_mode_count = planned_mode_count - 1?
Согласно методике, бурение уже включает одно посещение режима. Поэтому если по паспорту нужно 4 режима, то после бурения останется 3.

6. Безопасность и ограничения
Фильтрация по типу чата: глобальные фильтры dp.message.filter(F.chat.type == "private") и аналогичный для callback-запросов гарантируют, что бот отвечает только в личных сообщениях. В группах он игнорирует любые сообщения, но реагирует на события изменения членства.

Проверка ролей: во всех хендлерах, требующих повышенных привилегий (например, /create_object), вызывается вспомогательная функция check_user_role, которая сверяет роль пользователя с разрешёнными.

Защита от конфликтов при создании объекта: перед привязкой чата повторно проверяется, что этот чат ещё не привязан (OrgObject.objects.filter(chat_id=chat_id).first()). Если чат уже занят, объект создаётся без привязки, пользователь получает предупреждение.

Обработка ошибок: в критических местах (отправка сообщения в чат, работа с БД) используются блоки try-except с логированием ошибок, но без прерывания основного потока (пользователь видит сообщение, что отчёт сохранён, но не отправлен).

7. Технологический стек и обоснование
Компонент	Версия	Почему выбран
Python	3.12	Современный, асинхронный, широко используется.
Django	5.0	Мощный ORM, встроенная админка, миграции, удобство работы с БД.
aiogram	3.4	Асинхронная библиотека для Telegram, удобная FSM, хорошая документация.
SQLite	(встроен)	Для разработки и тестирования; легко заменить на PostgreSQL.
asgiref	-	Обеспечивает синхронизацию async и sync кода (sync_to_async).
python-dotenv	-	Управление переменными окружения (токен, секретный ключ).
Почему Django, а не более лёгкий фреймворк (Flask, FastAPI)?
Наличие встроенной админки и ORM с миграциями значительно ускоряет разработку и позволяет быстро создавать прототипы. Админка используется для ручного управления данными на этапе MVP.

Почему весь код бота в одном файле?
Упрощает навигацию и отладку на начальном этапе. 

8. Заключение
Текущая версия бота полностью реализует все ключевые требования: создание объектов и скважин, сбор отчётов по этапам с учётом аварий, автоматический переход этапов, отправка медиафайлов. Архитектура построена с учётом возможного расширения:

Легко добавить новые этапы – достаточно создать соответствующую запись в Form и настроить логику переходов в save_report.

Легко изменить набор полей в существующих этапах – через админку Django.

Легко добавить команды для администраторов (например, редактирование скважин, просмотр статистики).

Конец документа

