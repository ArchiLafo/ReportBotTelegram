from django.db import models
from django.utils import timezone


class TgRole(models.TextChoices):
    DEVELOPER = 'developer', 'Разработчик'
    ADMIN = 'admin', 'Администратор'
    FILLER = 'filler', 'Заполнитель'


class TgUser(models.Model):
    tg_user_id = models.BigIntegerField(unique=True, verbose_name='ID в Telegram')
    full_name = models.CharField(max_length=255, blank=True, verbose_name='Полное имя')
    role = models.CharField(
        max_length=20,
        choices=TgRole.choices,
        default=TgRole.FILLER,
        verbose_name='Роль'
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='Дата регистрации')

    class Meta:
        verbose_name = 'Пользователь Telegram'
        verbose_name_plural = 'Пользователи Telegram'

    def __str__(self):
        return f"{self.full_name or self.tg_user_id} [{self.role}]"


class KnownChatType(models.TextChoices):
    GROUP = 'group', 'Группа'
    SUPERGROUP = 'supergroup', 'Супергруппа'


class KnownChat(models.Model):
    chat_id = models.BigIntegerField(unique=True, verbose_name='ID чата')
    title = models.CharField(max_length=255, blank=True, verbose_name='Название')
    chat_type = models.CharField(
        max_length=20,
        choices=KnownChatType.choices,
        verbose_name='Тип чата'
    )
    is_active = models.BooleanField(default=True, verbose_name='Бот в чате')
    last_seen_at = models.DateTimeField(default=timezone.now, verbose_name='Последняя активность')

    class Meta:
        verbose_name = 'Известный чат'
        verbose_name_plural = 'Известные чаты'

    def __str__(self):
        return f"{self.title} ({self.chat_type}) {self.chat_id}"


class ObjectStatus(models.TextChoices):
    ACTIVE = 'active', 'Активен'
    CLOSED = 'closed', 'Закрыт'


class ObjectType(models.TextChoices):
    PRIVATE = 'private', 'Частный'
    FEDERAL = 'federal', 'Федеральный'


class OrgObject(models.Model):
    name = models.CharField(max_length=255, verbose_name='Название объекта')
    object_type = models.CharField(
        max_length=20,
        choices=ObjectType.choices,
        default=ObjectType.PRIVATE,
        verbose_name='Тип объекта'
    )
    passport_json = models.JSONField(default=dict, blank=True, verbose_name='Паспорт (JSON)')
    status = models.CharField(
        max_length=20,
        choices=ObjectStatus.choices,
        default=ObjectStatus.ACTIVE,
        verbose_name='Статус'
    )
    chat = models.OneToOneField(
        KnownChat,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bound_object',
        verbose_name='Привязанный чат'
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name='Дата создания')

    class Meta:
        verbose_name = 'Объект'
        verbose_name_plural = 'Объекты'

    def __str__(self):
        return self.name


class WellStage(models.TextChoices):
    DRILLING = 'drilling', 'Бурение'
    PUMPING = 'pumping', 'ОФР'
    MAINTENANCE = 'maintenance', 'Сопровождение'
    LIQUIDATION = 'liquidation', 'Ликвидация'
    COMPLETED = 'completed', 'Завершена'
    FORCE_MAJEURE = 'force_majeure', 'Форс-мажор'  # новый статус для заморозки


class WellStatus(models.TextChoices):
    ACTIVE = 'active', 'Активна'
    CLOSING_PENDING = 'closing_pending', 'Ожидает подтверждения закрытия'
    CLOSED = 'closed', 'Закрыта'


class Well(models.Model):
    object = models.ForeignKey(
        OrgObject,
        on_delete=models.CASCADE,
        related_name='wells',
        verbose_name='Объект'
    )
    maintenance_needed = models.BooleanField(
        default=False,
        verbose_name='Требуется сопровождение'
    )
    previous_stage = models.CharField(
        max_length=20,
        choices=WellStage.choices,
        null=True,
        blank=True,
        verbose_name='Предыдущий этап (для форс-мажора)'
    )
    name = models.CharField(max_length=100, verbose_name='Номер/название скважины')
    planned_depth_m = models.FloatField(null=True, blank=True, verbose_name='Плановая глубина (м)')
    current_depth_m = models.FloatField(null=True, blank=True, verbose_name='Текущая глубина (м)')
    # Поля для этапов
    stage = models.CharField(
        max_length=20,
        choices=WellStage.choices,
        default=WellStage.DRILLING,
        verbose_name='Текущий этап'
    )
    planned_pumping_hours = models.FloatField(null=True, blank=True, verbose_name='Плановая длительность ОФР (часы)')
    pumping_started_at = models.DateTimeField(null=True, blank=True, verbose_name='Начало ОФР')
    pumping_completed_at = models.DateTimeField(null=True, blank=True, verbose_name='Завершение ОФР')
    maintenance_started_at = models.DateTimeField(null=True, blank=True, verbose_name='Начало сопровождения')
    maintenance_completed_at = models.DateTimeField(null=True, blank=True, verbose_name='Завершение сопровождения')
    liquidation_completed_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата ликвидации')
    total_pumping_hours = models.FloatField(default=0.0, verbose_name='Накоплено часов ОФР')
    # Существующие поля
    status = models.CharField(
        max_length=30,
        choices=WellStatus.choices,
        default=WellStatus.ACTIVE,
        verbose_name='Статус'
    )
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата закрытия')

    class Meta:
        verbose_name = 'Скважина'
        verbose_name_plural = 'Скважины'
        ordering = ['object', 'name']

    def __str__(self):
        return f"{self.object.name} / скв. {self.name}"


class FieldType(models.TextChoices):
    TEXT = 'text', 'Текст'
    NUMBER = 'number', 'Число'
    CHECKBOX = 'checkbox', 'Флаг'
    SELECT = 'select', 'Список'


class Form(models.Model):
    code = models.CharField(max_length=100, unique=True, verbose_name='Код формы')
    title = models.CharField(max_length=255, verbose_name='Название')
    is_active = models.BooleanField(default=True, verbose_name='Активна')

    class Meta:
        verbose_name = 'Форма'
        verbose_name_plural = 'Формы'

    def __str__(self):
        return f"{self.code}: {self.title}"


class FormField(models.Model):
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='fields', verbose_name='Форма')
    key = models.CharField(max_length=100, verbose_name='Ключ поля (латиница)')
    label = models.CharField(max_length=255, verbose_name='Название поля')
    type = models.CharField(max_length=20, choices=FieldType.choices, verbose_name='Тип поля')
    required = models.BooleanField(default=False, verbose_name='Обязательное')
    options_json = models.JSONField(default=list, blank=True, verbose_name='Варианты (для списка)')
    order_index = models.IntegerField(default=0, verbose_name='Порядок')
    is_active = models.BooleanField(default=True, verbose_name='Активно')

    class Meta:
        unique_together = ('form', 'key')
        ordering = ('order_index', 'id')
        verbose_name = 'Поле формы'
        verbose_name_plural = 'Поля формы'

    def __str__(self):
        return f"{self.form.code}.{self.key}"


class Report(models.Model):
    object = models.ForeignKey(OrgObject, on_delete=models.PROTECT, verbose_name='Объект')
    well = models.ForeignKey(Well, on_delete=models.PROTECT, null=True, blank=True, verbose_name='Скважина')
    author = models.ForeignKey(TgUser, on_delete=models.PROTECT, verbose_name='Автор')
    report_date = models.DateField(default=timezone.localdate, verbose_name='Дата отчёта')
    stage = models.CharField(
        max_length=20,
        choices=WellStage.choices,
        null=True,
        blank=True,
        verbose_name='Этап отчёта'
    )
    payload_json = models.JSONField(default=dict, blank=True, verbose_name='Данные полей')
    message_text = models.TextField(blank=True, verbose_name='Текст сообщения')
    tg_chat_id = models.BigIntegerField(null=True, blank=True, verbose_name='ID чата отправки')
    tg_message_id = models.BigIntegerField(null=True, blank=True, verbose_name='ID сообщения')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='Время создания')

    class Meta:
        verbose_name = 'Отчёт'
        verbose_name_plural = 'Отчёты'
        ordering = ['-created_at']

    def __str__(self):
        return f"Отчёт #{self.id} {self.report_date} {self.object.name}"


class CloseRequestStatus(models.TextChoices):
    PENDING = 'pending', 'Ожидает'
    APPROVED = 'approved', 'Подтверждено'
    REJECTED = 'rejected', 'Отклонено'


class CloseRequest(models.Model):
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name='close_requests', verbose_name='Отчёт')
    object = models.ForeignKey(OrgObject, on_delete=models.PROTECT, verbose_name='Объект')
    well = models.ForeignKey(Well, on_delete=models.PROTECT, verbose_name='Скважина')
    initiator = models.ForeignKey(TgUser, on_delete=models.PROTECT, related_name='initiated_close_requests', verbose_name='Инициатор')
    status = models.CharField(
        max_length=20,
        choices=CloseRequestStatus.choices,
        default=CloseRequestStatus.PENDING,
        verbose_name='Статус'
    )
    decided_by = models.ForeignKey(
        TgUser,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='decided_close_requests',
        verbose_name='Кто решил'
    )
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата решения')

    class Meta:
        verbose_name = 'Запрос на закрытие'
        verbose_name_plural = 'Запросы на закрытие'
        ordering = ['-id']

    def __str__(self):
        return f"Запрос #{self.id} {self.well} ({self.status})"