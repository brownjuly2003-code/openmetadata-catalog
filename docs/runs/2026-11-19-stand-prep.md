# Подготовка стенда OpenMetadata 2.0.8

19 ноября 2026 года подготовлен стенд для одного автоматического прогона. **Стенд не запускался.** Ни в одной среде не поднимались ни OpenMetadata, ни его источники. Ниже записано, из чего собран стенд и откуда взяты решения. Здесь же перечислено, что остаётся непроверенным.

## Источник compose-файла

Сервисы OpenMetadata взяты из файла `docker-compose-postgres.yml`, приложенного к релизу [2.0.8-release](https://github.com/open-metadata/OpenMetadata/releases/tag/2.0.8-release) от 13.11.2026. Файл — 30 753 байта, sha256 `b0013093c8a78f7e877fb258fd7ba688adac3effdc65958569a5f9bd310d29ee`. Он сохранён без изменений в [stand/upstream](../../stand/upstream/docker-compose-postgres.yml). Тест `tests/test_stand.py` сверяет с ним образы, команды, переменные, проверки здоровья и зависимости сервисов стенда. Переменные сервера и миграции совпадают со всеми 128 и 137 переменными релиза, кроме `PIPELINE_SERVICE_CLIENT_ENABLED`.

| Отклонение от релиза | Причина |
|---|---|
| Образы `openmetadata/server` и `openmetadata/postgresql` с Docker Hub, а не через `docker.getcollate.io` | `docker.getcollate.io` авторизует через `auth.docker.io` и ответил HTTP 429 по лимиту анонимных загрузок Docker Hub. Digest взят из реестра Docker Hub для того же тега; через прокси его сверить не удалось |
| `PIPELINE_SERVICE_CLIENT_ENABLED: "false"`, сервис `ingestion` не поднимается | Сбор и проверки запускает внешний CLI. См. следующий раздел |
| Нет `container_name`, фиксированной подсети и опубликованных портов базы и Elasticsearch; сервер слушает только `127.0.0.1` | Чтобы стенд не конфликтовал с другими контейнерами и не был виден снаружи |
| Именованный том вместо `./docker-volume` | `docker compose down -v` удаляет всё, на диске не остаётся файлов базы |
| У проверки здоровья сервера добавлены `interval`, `timeout`, `retries`, `start_period` | В релизе задана только команда. С параметрами Docker по умолчанию сервер мог бы получить статус `unhealthy` до конца запуска |

## Почему без контейнера ingestion

Документация 2.0.x, [External Ingestion](https://docs.open-metadata.org/v2.0.x/deployment/ingestion/external), описывает запуск конвейеров только снаружи. Для этого клиент конвейеров выключают через `enabled: false` или `PIPELINE_SERVICE_CLIENT_ENABLED=false`. Там же сказано, что перестанут работать некоторые API и мониторы, связанные с клиентом конвейеров, например Airflow. В коде сервера 2.0.8 `PipelineServiceClientFactory.java` при `enabled == false` пишет в журнал «Pipeline Service Client is disabled. Skipping initialization.» и не создаёт клиент. Значит, для сервера Airflow не нужен. Обе процедуры — `metadata ingest` и `metadata test` — запускает `run.sh` из виртуального окружения с `openmetadata-ingestion[postgres,clickhouse]==2.0.8.0`.

## Образы

| Сервис | Образ и тег | Digest (индекс) | Сжатый размер linux/amd64 |
|---|---|---|---|
| `openmetadata-server`, `execute-migrate-all` | `openmetadata/server:2.0.8` | `sha256:df3c3fef…0ab4` | 0,48 ГБ |
| `postgresql` | `openmetadata/postgresql:2.0.8` | `sha256:a3d56116…a904` | 0,16 ГБ |
| `elasticsearch` | `docker.elastic.co/elasticsearch/elasticsearch:9.3.0` | `sha256:4f6bdcb7…cb60` | 0,74 ГБ |
| `source-postgres` | `postgres:16.10-alpine` | `sha256:02966064…2297` | 0,11 ГБ |
| `source-clickhouse` | `clickhouse/clickhouse-server:24.8.14.39` | `sha256:1ffa82ed…597b` | 0,18 ГБ |

Полные значения digest — в [stand/docker-compose.yml](../../stand/docker-compose.yml). Размеры — сумма слоёв манифеста linux/amd64 из реестров. Образ ClickHouse совпадает с образом, уже закреплённым в `tests/quality_sql_cases.py`. PostgreSQL 16.10 — версия проекта P17.

Devcontainer: `mcr.microsoft.com/devcontainers/base:3.0.13-ubuntu24.04` закреплён по digest из реестра MCR. Фичи `docker-in-docker:4.1.3`, `python:1.8.1` (Python 3.11) и `sshd:1.1.0` — последние версии в `ghcr.io/devcontainers/features` на 19.11.2026. Версия самого Docker Engine не закреплена: фича ставит последнюю.

## Ресурсы (оценка, не измерение)

- Документация [Local Docker Deployment](https://docs.open-metadata.org/v2.0.x/quick-start/local-docker-deployment) указывает минимум 6 GiB памяти, 4 vCPU и Docker Compose v2.1.1 или новее.
- Образы — около 1,7 ГБ в сжатом виде. В распакованном виде, по оценке, 4–5 ГБ, плюс виртуальное окружение клиента. Порог свободного места в `run.sh` — 15 ГБ.
- Память, оценка по настройкам релиза: куча Elasticsearch 1 ГБ (`-Xms1024m -Xmx1024m`) и куча сервера 1 ГБ (`-Xmx1G -Xms1G`), плюс накладные расходы JVM, две базы PostgreSQL, ClickHouse и процесс CLI. Порог в `run.sh` — 6 ГБ, по документации. Фактическое потребление запишет `docker stats --no-stream`.

## Фикстуры источников

Схемы скопированы байт в байт: заказы — из проекта P17 (`metabase-order-picking`, коммит `d21a222`), маркетинговые таблицы — из P16 (`marketing-funnel-datalens`, коммит `3d60450`). Данные сформированы скриптом [build_data.py](../../stand/fixtures/build_data.py) из архивных синтетических `inputs.json` тех же коммитов: 200 заказов, 488 событий, 4 строки расходов. Порядок загрузки ClickHouse повторяет `scripts/load_clickhouse.py` из P16. Коммиты, исходные пути и контрольные суммы записаны в [SOURCES.json](../../stand/fixtures/SOURCES.json). Тест `test_fixture_sources_and_checksums` сверяет SHA-256 файлов этого репозитория с манифестом и проверяет только формат коммита (40 шестнадцатеричных символов). Происхождение из файлов P16 и P17 на указанных коммитах этот тест не доказывает. Его проверили отдельно при втором аудите 19.11: SHA-256 пяти исходных файлов и двух `inputs.json` в этих коммитах совпали с манифестом, а `build_data.py` из этих `inputs.json` воспроизвёл три производных файла байт в байт ([второй аудит](2026-11-19-second-audit.md)).

## Пути REST API

| Запрос | Источник | Статус |
|---|---|---|
| `POST /v1/users/login` (`email`, пароль в base64) → `accessToken` | Код 2.0.8: `UserResource.java`, `LoginRequest`, `JwtResponse.java` | В справочнике API 2.0.x не найден |
| `GET /v1/bots/name/{name}` → `botUser.id` | Код 2.0.8: `BotResource.java` | В справочнике API 2.0.x не найден |
| `GET /v1/users/auth-mechanism/{id}` → `config.JWTToken` | Код 2.0.8: `UserResource.java`, схема `jwtAuth` | В справочнике API 2.0.x не найден. Документация берёт токен из Settings → Bots |
| `GET /v1/teams/name/{fqn}`, `PUT /v1/teams` | api-reference/teams-and-users/teams: retrieve, update | Документация |
| `GET /v1/glossaries/name/{fqn}`, `PUT /v1/glossaries` | api-reference/governance/glossaries: retrieve, update | Документация |
| `GET /v1/glossaryTerms/name/{fqn}`, `PUT /v1/glossaryTerms` | api-reference/governance/glossary-terms: retrieve, update | Документация |
| `GET /v1/tables/name/{fqn}?fields=owners,tags,columns&include=all` | api-reference/data-assets/tables/retrieve | Документация |
| `PATCH /v1/tables/{id}`, `application/json-patch+json` | api-reference/data-assets/tables/update | Документация; пример есть только для `/description` |
| Операции JSON Patch `/owners`, `/tags`, `/columns/{i}/tags` | RFC 6902, поля `owners` и `tags` из схемы таблицы, `tagLabel` (`tagFQN`, `source`, `labelType`, `state`) | Не подтверждено примерами документации |
| `GET /v1/lineage/table/name/{fqn}?upstreamDepth=1&downstreamDepth=0` | api-reference/lineage/get | Документация |
| `PUT /v1/lineage` с `edge.fromEntity` и `edge.toEntity` | api-reference/lineage/add | Документация |
| `GET /v1/dataQuality/testCases?entityLink=…&fields=testCaseResult,testDefinition` | api-reference/data-quality/test-cases/list, results | Документация |

Термины глоссария создаются в статусе `Approved`, если у глоссария нет рецензентов (`GlossaryTermRepository.java`, 2.0.8). Назначить на объект можно только термин в статусе `Approved` (`TagLabelUtil.checkGlossaryTermsApproved`). Рецензенты в `catalog.json` не заданы. Термин в другом статусе `apply_catalog.py` считает ошибкой.

## Что покажет первый прогон и где вероятнее всего сломается

Прогон должен проверить допущения, которые нельзя закрыть без каталога. Это вид полных имён ClickHouse, тип `View` у `order_facts`, сохранность ручных описаний и назначений при повторном сборе, семантика `strategy: COUNT` и запись `resultRowCount`. Самые вероятные места сбоя, по убыванию:

1. `docker compose up --wait` вернёт ошибку из-за одноразового контейнера миграций, хотя тот завершился с кодом 0. Это неофициальные сообщения пользователей, документацией не подтверждено. На этот случай `run.sh` проверяет код миграции и здоровье сервисов напрямую. Причина записывается в `up-fallback.txt`.
2. Получение токена бота (шаг 6): пути `login` и `auth-mechanism` взяты из кода, а не из документации.
3. PATCH меток на столбцах (`/columns/{i}/tags`): путь по RFC 6902, примера в документации нет.
4. FQN ClickHouse: если коннектор назовёт базу не `default`, прогон остановится на шаге 8. Там `completeness.py --require-present` выводит отчёт в `before_completeness.json` и завершается с кодом 4, пока ничего не записано в каталог (до 19.11, второго аудита, шаг 8 этого не делал, и первая запись — команды и термины — успевала пройти на шаге 9). Тогда придётся поправить `catalog.json` и `workflows/quality-attribution.yaml`.
5. Нехватка памяти при одновременной работе Elasticsearch, сервера и CLI.

## Проверка без запуска

Новые тесты не запускают контейнеров. `tests/test_stand.py` проверяет compose-файл против релиза, закрепление образов по тегу и digest, локальные порты, переменные `.env.example`, контрольные суммы фикстур, существование путей из `run.sh`, `bash -n` для трёх shell-скриптов (только на Linux) и devcontainer. `tests/test_apply_catalog.py` и `tests/test_stand_export.py` запускают скрипты против HTTP-сервера в памяти. Проверяется, что уходит в запросах, что повторное применение отправляет только GET и что ошибка API останавливает скрипт с кодом 2. Итоговая сверка тоже проверена на синтетическом наборе выгрузок. Сервер в памяти моделирует только нужные скриптам ответы и о поведении настоящего OpenMetadata ничего не говорит.

### Прогон CI с этими тестами

Полный прогон `openmetadata-catalog_20261119-100831` на коммите `092f06b`: обе задачи — `JOB_ok` (`workflows` — 89 с, `quality-sql` — 20 с). Прошёл 61 тест: 14 (выборка и отчёт полноты), 40 (файлы стенда, применение каталога и выгрузки на подменённом API), 5 (конфигурации и модели клиента), 2 (SQL проверок качества). Стенд в этом прогоне не запускался. [Сводка CI и журналы pytest](stand-prep-ci.zip) — `summary.tsv` и четыре журнала pytest этого прогона, забраны с раннера при втором аудите 19.11.

До него три прогона того же дня завершились FAIL, и причины исправлены отдельными коммитами:

- `…094101` и `…095937` — тест `test_source_images_match_the_versions_already_pinned` искал образ ClickHouse одной строкой, а в `tests/quality_sql_cases.py` он записан в две (исправление — `d5184f7`);
- `…100305` — `ModuleNotFoundError: No module named 'pytz'`: пакет `openmetadata-ingestion==2.0.8.0` импортирует `pytz`, но не объявляет его в зависимостях; CI теперь ставит пакет с теми же extras, что README и `stand/run.sh` (`[postgres,clickhouse]`), а `pytz` приходит с драйвером ClickHouse (исправление — `092f06b`). Прежние прогоны проходили без extras; какая транзитивная зависимость перестала приносить `pytz`, не выяснялось.

