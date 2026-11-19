# Проверка конфигураций OpenMetadata, 18 ноября 2026

Полный прогон `openmetadata-catalog_20261118-185053` завершился с `JOB_ok` на Linux-раннере iMac. Прошли Ruff и четыре теста на Python 3.11. [Сводка CI и журналы pytest](workflows-ci.zip)

| Проверка | Результат |
|---|---|
| Состав выборки PostgreSQL | Схема `picking`, таблица `orders` и представление `order_facts`. Имя `orders_archive` отсекается |
| Состав выборки ClickHouse | В `marketing_jan` выбраны `events`, `spend`, `attribution`, `channel_metrics`, `user_funnel`. Временные таблицы и `query_log` из контрольного набора отсекаются |
| Штатная загрузка PostgreSQL | После подстановки тестовых переменных парсер создал `PostgresConnection`. Штатные фильтры пропустили `order_facts` и исключили `order_facts_archive` |
| Штатная загрузка ClickHouse | Парсер создал `ClickhouseConnection`. Штатные фильтры пропустили `channel_metrics` и исключили `channel_metrics_archive` |

До добавления конфигураций два теста состава выборки падали на отсутствии файлов в прогоне `openmetadata-catalog_20261118-184909`. В успешном прогоне использовался клиент `openmetadata-ingestion==2.0.8.0` с загрузчиком YAML и парсером полного workflow. [Тесты](../../tests) · [Версия клиента](https://pypi.org/project/openmetadata-ingestion/2.0.8.0/)

Ожидаемые имена семи объектов отдельно сверены с DDL P17 и P16. Адреса и учётные данные в тестах фиктивные. Доступность источников, создание карточек и повторный сбор предстоит проверить на экземпляре OpenMetadata.
