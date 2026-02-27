from django.contrib import admin
from .models import (
    TgUser, KnownChat, OrgObject, Well,
    Form, FormField, Report, CloseRequest
)

@admin.register(TgUser)
class TgUserAdmin(admin.ModelAdmin):
    list_display = ('tg_user_id', 'full_name', 'role', 'is_active', 'created_at')
    list_filter = ('role', 'is_active')
    search_fields = ('full_name', 'tg_user_id')

@admin.register(KnownChat)
class KnownChatAdmin(admin.ModelAdmin):
    list_display = ('chat_id', 'title', 'chat_type', 'is_active', 'last_seen_at')
    list_filter = ('chat_type', 'is_active')

@admin.register(OrgObject)
class OrgObjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'chat', 'created_at')
    list_filter = ('status',)
    search_fields = ('name',)

@admin.register(Well)
class WellAdmin(admin.ModelAdmin):
    list_display = ('object', 'name', 'planned_depth_m', 'current_depth_m', 'status', 'closed_at')
    list_filter = ('status', 'object')
    search_fields = ('name',)

@admin.register(Form)
class FormAdmin(admin.ModelAdmin):
    list_display = ('code', 'title', 'is_active')
    list_filter = ('is_active',)

@admin.register(FormField)
class FormFieldAdmin(admin.ModelAdmin):
    list_display = ('form', 'key', 'label', 'type', 'required', 'order_index', 'is_active')
    list_filter = ('form', 'type', 'is_active')
    ordering = ('form', 'order_index')

@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ('id', 'object', 'well', 'author', 'report_date', 'created_at')
    list_filter = ('object', 'author', 'report_date')
    search_fields = ('message_text',)

@admin.register(CloseRequest)
class CloseRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'well', 'initiator', 'status', 'decided_by', 'decided_at')
    list_filter = ('status', 'object')