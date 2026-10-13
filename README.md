# toolkit-session-search — Toolkit

Приватный репозиторий: https://github.com/pikiby/toolkit-session-search

Имя пакета и команды — `session-search`. Целевая система проверки — Linux Mint 22.3;
совместимость с другими версиями Linux не подтверждена.

Самостоятельный поиск сессий Codex: stdio MCP и независимый фоновый индексатор.
Источники — `~/.codex/sessions` и `~/.codex/archived_sessions`; исходная переписка
и служебная база Codex не изменяются. Архивирование, удаление и объединение
сессий не входят в инструмент. Семантическое совпадение не доказывает, что
найденная задача ещё не завершена: прочитайте последующую переписку.

## Исходники

Модули находятся в `src/session_search`:

| Модуль | Назначение |
| --- | --- |
| `reader.py` | Чтение завершённых строк JSONL и сообщений с байтовыми координатами. |
| `store.py` | SQLite, позиции чтения, фрагменты, постоянная очередь и sqlite-vec. |
| `embedding.py` | Закреплённая offline CPU-модель и эмбеддинги. |
| `indexer.py` | Наблюдение файлов, восстановление, обработка очереди и Unix-сокет. |
| `server.py` | Четыре инструмента stdio MCP. |
| `tui.py`, `tui_backend.py` | Терминальный интерфейс, проверка состояния и управление user-service. |

## Инструменты MCP

| Инструмент | Аргументы и результат |
| --- | --- |
| `session_search` | Обязательный `query`; `mode=semantic\|literal` (по умолчанию semantic), `state=active\|archived\|all` (all), `limit` 1–200 (20), `include_snippets` (false), `codex_home`. Возвращает совпадения и координаты исходных фрагментов. |
| `session_info` | Обязательный `session` (ID, имя, имя файла или путь); `state`, `codex_home`, `include_messages` (true). Возвращает метаданные и краткие фрагменты пользовательских сообщений. |
| `session_resume_command` | Обязательный `session`; `state`, `codex_home`. Возвращает команду открытия выбранной активной сессии. |
| `index_status` | Необязательный `codex_home`. Возвращает состояние очереди и готовность индексатора. |

`codex_home` по умолчанию берётся из `CODEX_HOME` или `~/.codex`.
В semantic-поиске `limit` ограничивает фрагменты, в literal — сессии.

## Сборка и установка

Требуется Python 3.12 или новее. Для сборки нужен уже установленный `dpkg-deb`:

```bash
./scripts/build-deb.sh
sudo dpkg -i dist/session-search_0.4.0_all.deb
```

Пакет содержит только Python-код, launcher, документацию и user-unit.
Установка не создаёт venv, не скачивает модель, не запускает сервис и не
переключает действующий сервис. В пакете нет maintainer scripts.
Код устанавливается в `/usr/lib/session-search/session_search`, launcher —
`/usr/bin/session-search`, unit — `/usr/lib/systemd/user/codex-session-index.service`.

## Python и offline-модель

По умолчанию используется существующее окружение
`~/.local/share/codex-session-index/venv/bin/python`. Альтернативный абсолютный
путь задаётся `SESSION_SEARCH_PYTHON`. Индексатору нужны версии из
`requirements-indexer.txt`; CPU PyTorch — `2.6.0+cpu`. MCP использует стандартную
библиотеку; semantic-запросы обслуживает индексатор через Unix-сокет.

**Установка зависимостей разрешается только отдельным явным решением.**
Сборка, установка .deb и запуск launcher не выполняют pip. Для нового окружения
сначала согласуйте создание venv, источники пакетов и установку зависимостей;
этот документ не выполняет и не подразумевает такие действия.

Переиспользуются уже имеющиеся локальные веса
`Qwen/Qwen3-Embedding-0.6B`, ревизия
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, размерность 1024:

```text
~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
```

Индексатор запускается offline без remote model code. Параметр `--model-path` принимает только этот закреплённый snapshot
в стандартном Hugging Face cache; произвольный каталог весов не принимается.
Весов в пакете нет; при их отсутствии требуется отдельное решение о получении.

## Сервис и переход с прежнего MCP-work

Сохраняются база `~/.local/share/codex-session-index/index.sqlite3`, очередь,
позиции чтения и формат векторов. Сокет —
`$XDG_RUNTIME_DIR/codex-session-index.sock`, при отсутствии переменной —
`~/.cache/codex-session-index/index.sock`. MCP учитывает
`CODEX_SESSION_INDEX_SOCKET`. Не запускайте два индексатора для одной базы.

Перед переходом установите пакет и проверьте внешнее окружение и веса.
Действующий пользовательский unit в `~/.config/systemd/user` имеет приоритет над
пакетным. Следующие команды являются явным ручным переходом, а не частью
установки. Остановите прежний сервис и сохраните прежний unit вне каталога
systemd; для отката восстановите его и выполните daemon-reload:

```bash
systemctl --user stop codex-session-index.service
mkdir -p ~/.local/share/codex-session-index/unit-backups
cp -a ~/.config/systemd/user/codex-session-index.service ~/.local/share/codex-session-index/unit-backups/codex-session-index.service.before-toolkit
cp /usr/lib/systemd/user/codex-session-index.service ~/.config/systemd/user/codex-session-index.service
systemctl --user daemon-reload
systemctl --user enable --now codex-session-index.service
```

Для новой установки без прежнего пользовательского unit достаточно
`systemctl --user daemon-reload` и
`systemctl --user enable --now codex-session-index.service`.
Существующие drop-in настройки проверьте через `systemctl --user cat`.
Для другого Python задайте `Environment=SESSION_SEARCH_PYTHON=/absolute/path`
в user drop-in через `systemctl --user edit codex-session-index.service`.

Сервис ограничен двумя потоками, `CPUQuota=200%`, `MemoryHigh=4G`,
`MemoryMax=5G`, `MemorySwapMax=0`; права новых данных — `UMask=0077`.
Автозапуск до входа пользователя не настраивается.

## Подключение MCP

Пример отдельной записи клиента Codex:

```toml
[mcp_servers.session_search]
command = "/usr/bin/session-search"
args = ["mcp"]
```

При необходимости добавьте `[mcp_servers.session_search.env]` с
`SESSION_SEARCH_PYTHON` и `CODEX_SESSION_INDEX_SOCKET`. После изменения
подключения перезапустите MCP-процесс клиента. CLI также допускает
`python -m session_search.server` и `python -m session_search.indexer` при
`PYTHONPATH=/usr/lib/session-search` либо `PYTHONPATH=src` из исходников.

## Проверка

```bash
session-search status
systemctl --user status codex-session-index.service
systemctl --user cat codex-session-index.service
systemctl --user show codex-session-index.service -p CPUQuotaPerSecUSec -p MemoryHigh -p MemoryMax -p MemorySwapMax
journalctl --user -u codex-session-index.service -n 30 --no-pager
PYTHONPATH=src ~/.local/share/codex-session-index/venv/bin/python -m unittest discover -s tests
```

Запуск сервиса не означает полной индексации. `index_pending=true` сообщает
о незавершённой обработке; остановленный индексатор возвращает ошибку.
Проверка сборки и распаковки .deb не является проверкой установки на Linux.

## Обновление и удаление

Обновление: установите новый .deb, затем явно перезапустите сервис и MCP-клиент.
Установка пакета сама не меняет уже работающий процесс. Если пользовательский
unit скопирован вручную, сравните его с новой пакетной версией перед заменой.

Удаление: сначала явно остановите и отключите user-service, затем
`sudo dpkg -r session-search`. Пользовательский unit, внешнее venv, offline-веса,
SQLite и исходные сессии сохраняются; удалять их следует только отдельным
решением. База содержит переписку: не публикуйте её, веса, venv и секреты в Git.

## Терминальный интерфейс

`session-search tui` показывает компактные английские строки прямо в терминале,
без рамок, панелей и полноэкранного режима. Используется установленный Textual 8.2.8.

1. `Indexing`: RUNNING — очередь обрабатывается; IDLE — индексатор запущен
   и ожидает изменения; STOPPED — выключен; ERROR — ошибка или статус недоступен.
2. CPU и RAM процесса индексатора с короткими графиками — только пока сервис
   работает. CPU100% соответствует одному ядру, RAM — RSS процесса.
3. Sessions — сессии; Fragments — обнаруженные фрагменты; Indexed — готовые
   эмбеддинги; Pending — незавершённая очередь, включая повторные попытки.

Стрелки ↑/↓ выбирают текстовый пункт Start/stop indexing или Quit; Enter выполняет
выбранную команду. `>` отмечает выбранный пункт. Также доступны `s` для запуска/остановки
индексатора и `q` для выхода. Выход закрывает только интерфейс.
MCP-команды проекта сохранены.
Неизвестные счётчики показываются как N/A; ноль — как0. Если индексатор выключен,
его база не открывается интерфейсом и счётчики могут быть недоступны.
История нагрузки хранится в памяти TUI; первый замер CPU неизвестен.

После установки нового .deb новый TUI открывается командой `session-search tui`.
Пакет сам не перезапускает сервис. Для применения обновлённого индексатора нужен
явный `systemctl --user restart codex-session-index.service`.
