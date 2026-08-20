-- LEO CRM: безопасное восстановление квоты Supabase после переполнения.
-- Запускайте разделы по очереди в Supabase SQL Editor.

-- 1. Сначала посмотреть, какие таблицы занимают место.
SELECT
    relname AS table_name,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
    pg_total_relation_size(relid) AS total_bytes
FROM pg_catalog.pg_statio_user_tables
ORDER BY total_bytes DESC
LIMIT 30;

-- 2. Удалить только избыточные технические истории. Товары, заказы, остатки,
-- закупки, актуальные состояния и активные задания не удаляются.
BEGIN;

DELETE FROM marketplace_raw_payloads WHERE id IN (
    SELECT id FROM (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY marketplace_account_id, payload_type, external_object_id
                ORDER BY received_at DESC, id DESC
            ) AS history_rank
        FROM marketplace_raw_payloads
        WHERE payload_type = 'order'
    ) AS ranked
    WHERE history_rank > 20
);

DELETE FROM dumping_runs WHERE id IN (
    SELECT id FROM (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY workspace_id, product_id
                ORDER BY id DESC
            ) AS history_rank
        FROM dumping_runs
        WHERE status NOT IN ('queued_local', 'leased_local')
    ) AS ranked
    WHERE history_rank > 100
);

DELETE FROM fast_dumping_jobs WHERE id IN (
    SELECT id FROM (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY workspace_id, product_id
                ORDER BY id DESC
            ) AS history_rank
        FROM fast_dumping_jobs
        WHERE completed_at IS NOT NULL
    ) AS ranked
    WHERE history_rank > 100
);

DELETE FROM product_test_jobs WHERE id IN (
    SELECT id FROM (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY workspace_id
                ORDER BY id DESC
            ) AS history_rank
        FROM product_test_jobs
        WHERE completed_at IS NOT NULL
    ) AS ranked
    WHERE history_rank > 40
);

COMMIT;

ANALYZE marketplace_raw_payloads;
ANALYZE dumping_runs;
ANALYZE fast_dumping_jobs;
ANALYZE product_test_jobs;

-- 3. Снова выполнить запрос размеров из раздела 1.
-- Если строки удалились, но физический размер не уменьшился, выполните каждую
-- следующую команду ОТДЕЛЬНЫМ запуском. VACUUM FULL временно блокирует только
-- указанную техническую таблицу и возвращает свободное место Supabase.

-- VACUUM (FULL, ANALYZE) marketplace_raw_payloads;
-- VACUUM (FULL, ANALYZE) dumping_runs;
-- VACUUM (FULL, ANALYZE) fast_dumping_jobs;
-- VACUUM (FULL, ANALYZE) product_test_jobs;

-- 4. Итоговый размер базы.
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
