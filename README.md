# Discord Control Bot

Discord-бот с полной системой управления сервером:

- модерация с mod-log и кейсами (и отменой по case id)
- warn-система с баллами и авто-наказаниями
- тикеты с экспортом переписки
- временные voice-комнаты
- планировщик объявлений/напоминаний/ивентов
- backup/restore данных
- удержание бота в заданном voice-канале

## 1. Подготовка в Discord

1. Создай приложение в [Discord Developer Portal](https://discord.com/developers/applications).
2. Во вкладке `Bot` скопируй токен.
3. Включи intents:
   - `SERVER MEMBERS INTENT`
   - `MESSAGE CONTENT INTENT`
4. Пригласи бота с правами:
   - `View Channels`
   - `Send Messages`
   - `Manage Messages`
   - `Manage Channels`
   - `Moderate Members`
   - `Kick Members`
   - `Ban Members`
   - `Move Members`
   - `Mute Members`
   - `Deafen Members`
   - `Manage Roles` (желательно)
   - `Connect`
   - `Speak` (опционально)

## 2. Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. Настройка

1. Скопируй `.env.example` в `.env`.
2. Заполни переменные:
   - `BOT_TOKEN` — токен бота.
   - `VOICE_CHANNEL_ID` — ID voice-канала, в котором бот должен сидеть постоянно.
   - `OWNER_USER_ID` — опционально, список ID через запятую (получат админ-доступ к командам, даже без admin роли).
   - `DB_PATH` — путь к sqlite-базе (по умолчанию `bot_data.sqlite3`).
   - `USE_PRIVILEGED_INTENTS` — `1` для полного режима (рекомендуется), `0` для ограниченного режима без privileged intents.

## 4. Запуск

```powershell
python -m bot
```

## 5. Базовые setup-команды

Перед использованием систем настрой каналы:

- `/setup_owner_hub` — создать 2 канала автоматически: публичный канал с кнопкой создания временного voice и приватный owner-admin канал (видят только ID из `OWNER_USER_ID`)
- `/bind_temp_create_channel channel:<#канал>` — привязать уже созданный канал для кнопки temp-room
- `/bind_owner_admin_channel channel:<#канал>` — привязать уже созданный owner-admin канал и сразу сделать его приватным

После `setup_owner_hub` в приватном owner-admin канале публикуются кнопочные панели.
Кнопки подписаны названиями команд и выполняют действия через модальные формы (без ввода slash-команд вручную).

- `/set_modlog channel:<#канал>`
- `/set_alert_channel channel:<#канал>`
- `/set_backup_channel channel:<#канал>`

## 6. Mod-Log и кейсы

Каждое действие пишет case в базу и (если настроено) в mod-log канал.

- `/case_info case_id:<id>`
- `/case_undo case_id:<id> reason:<текст>`

Поддерживаемая авто-отмена: `ban`, `timeout`, `voice_ban`, `voice_mute`, `voice_deafen`, а также auto-наказания от warn.

## 7. Moderation команды

- `/ban member:<@user> reason:<text> delete_days:<0-7>`
- `/unban user_id:<id> reason:<text>`
- `/kick member:<@user> reason:<text>`
- `/timeout member:<@user> minutes:<1-40320> reason:<text>`
- `/untimeout member:<@user> reason:<text>`
- `/voice_ban member:<@user> reason:<text>`
- `/voice_unban member:<@user> reason:<text>`
- `/voice_mute member:<@user> reason:<text>`
- `/voice_unmute member:<@user> reason:<text>`
- `/voice_deafen member:<@user> reason:<text>`
- `/voice_undeafen member:<@user> reason:<text>`
- `/voice_move member:<@user> channel:<voice> reason:<text>`
- `/clear amount:<1-200> channel:<#канал>`
- `/lock channel:<#канал> reason:<text>`
- `/unlock channel:<#канал> reason:<text>`

## 8. Warn система

- `/warn member:<@user> points:<1-10> reason:<text>`
- `/unwarn warn_id:<id> reason:<text>`
- `/warns member:<@user>`

По умолчанию авто-пороги:

- 3 points -> timeout
- 5 points -> voice_ban
- 7 points -> ban

## 9. Ticket-система

Команды:

- `/set_ticket_category category:<категория>`
- `/set_ticket_log channel:<#канал>`
- `/set_ticket_support role:<@роль>`
- `/ticket_panel channel:<#канал>`
- `/ticket_close reason:<text>`

Через кнопку создается приватный тикет-канал, при закрытии делается экспорт переписки в txt.

## 10. Temp Voice-комнаты

Команды настройки:

- `/set_temp_lobby channel:<voice>`
- `/set_temp_category category:<категория>`

Логика:

- пользователь заходит в lobby-канал;
- бот создает личную комнату и переносит туда;
- пустая временная комната удаляется автоматически.
- либо пользователь нажимает кнопку в public temp-channel, и бот создает личную комнату сразу.

Кнопки в public temp-channel:

- `Создать комнату` — открывает форму с параметрами: название, лимит пользователей, режим `open`/`private`, список пользователей для доступа в private-комнату
- `Добавить доступ` — добавить пользователей в свою private-комнату (id/@/name)
- `Убрать доступ` — убрать доступ у пользователей
- `Сделать комнату open`
- `Сделать комнату private`

Команды владельца комнаты:

- `/room_lock`
- `/room_unlock`
- `/room_rename name:<название>`
- `/room_limit limit:<0-99>`

## 11. Планировщик

- `/schedule_reminder minutes:<n> message:<text> channel:<#канал?>`
- `/schedule_every minutes:<n> message:<text> channel:<#канал>`
- `/schedule_at when_utc:<YYYY-MM-DD HH:MM> message:<text> channel:<#канал>`
- `/schedule_list`
- `/schedule_remove schedule_id:<id>`

## 12. Резервные копии

- `/backup_create` — экспорт всех данных гильдии в JSON.
- `/backup_restore file:<json>` — восстановление из JSON.

## 13. Owner Admin Panel (кнопки)

В приватном owner-admin канале публикуются кнопки команд:

- Moderation 1: `ban`, `kick`, `timeout`, `voice_ban`, `warn`
- Moderation 2: `unban`, `untimeout`, `voice_unban`, `unwarn`, `clear`
- Config: `lock`, `unlock`, `set_modlog`, `set_alert_channel`, `set_backup_channel`
- Scheduler: `schedule_reminder`, `schedule_every`, `schedule_remove`
- Core: `Создать backup`, `Переопубликовать панели`, `Синхр. команд`

Каждая кнопка открывает форму параметров и выполняет действие сразу.

Можно настроить канал, куда backup будет отправляться автоматически: `/set_backup_channel`.

## 14. Полезно знать

- Если токен бота когда-либо попал в публичный доступ, сразу перевыпусти токен в Discord Developer Portal.
- Если получаешь `PrivilegedIntentsRequired`, включи в Discord Portal: `SERVER MEMBERS INTENT` и `MESSAGE CONTENT INTENT`. Временный обход: `USE_PRIVILEGED_INTENTS=0`.
