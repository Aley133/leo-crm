-- LEO CRM: восстановление квоты Supabase без длинного DELETE.
-- ВАЖНО: выделяйте мышкой и запускайте только ОДИН раздел за раз.

-- 1. Диагностика: этот SELECT должен завершиться быстро.
SELECT
    relname AS table_name,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
    pg_total_relation_size(relid) AS total_bytes
FROM pg_catalog.pg_statio_user_tables
ORDER BY total_bytes DESC
LIMIT 30;

-- Если DELETE сообщает read-only, один раз выполните отдельно:
-- SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE;

-- 2A. История JSON заказов: удаляет максимум 500 строк за запуск.
-- Повторяйте ТОЛЬКО этот раздел, пока deleted_rows не станет 0.
WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY
                workspace_id,
                marketplace_account_id,
                payload_type,
                external_object_id
            ORDER BY received_at DESC, id DESC
        ) AS history_rank
    FROM marketplace_raw_payloads
    WHERE payload_type = 'order'
),
doomed AS (
    SELECT id
    FROM ranked
    WHERE history_rank > 20
    ORDER BY id
    LIMIT 500
),
deleted AS (
    DELETE FROM marketplace_raw_payloads AS target
    USING doomed
    WHERE target.id = doomed.id
    RETURNING target.id
)
SELECT COUNT(*) AS deleted_rows FROM deleted;

-- 2B. Обычный dumping: максимум 1000 строк за запуск.
-- Повторяйте раздел, пока deleted_rows не станет 0.
WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, product_id
            ORDER BY id DESC
        ) AS history_rank
    FROM dumping_runs
    WHERE status NOT IN ('queued_local', 'leased_local')
),
doomed AS (
    SELECT id FROM ranked WHERE history_rank > 100 ORDER BY id LIMIT 1000
),
deleted AS (
    DELETE FROM dumping_runs AS target
    USING doomed
    WHERE target.id = doomed.id
    RETURNING target.id
)
SELECT COUNT(*) AS deleted_rows FROM deleted;

-- 2C. Fast Dumping: максимум 1000 завершённых заданий за запуск.
-- Активные задания не удаляются. Повторяйте до deleted_rows = 0.
WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, product_id
            ORDER BY id DESC
        ) AS history_rank
    FROM fast_dumping_jobs
    WHERE completed_at IS NOT NULL
),
doomed AS (
    SELECT id FROM ranked WHERE history_rank > 100 ORDER BY id LIMIT 1000
),
deleted AS (
    DELETE FROM fast_dumping_jobs AS target
    USING doomed
    WHERE target.id = doomed.id
    RETURNING target.id
)
SELECT COUNT(*) AS deleted_rows FROM deleted;

-- 2D. Тест товара: максимум 1000 завершённых заданий за запуск.
-- Повторяйте до deleted_rows = 0.
WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id
            ORDER BY id DESC
        ) AS history_rank
    FROM product_test_jobs
    WHERE completed_at IS NOT NULL
),
doomed AS (
    SELECT id FROM ranked WHERE history_rank > 40 ORDER BY id LIMIT 1000
),
deleted AS (
    DELETE FROM product_test_jobs AS target
    USING doomed
    WHERE target.id = doomed.id
    RETURNING target.id
)
SELECT COUNT(*) AS deleted_rows FROM deleted;

-- 3. После того как все четыре раздела вернули 0, выполните команды
-- ПО ОДНОЙ. VACUUM FULL возвращает физическое место и блокирует только
-- указанную техническую таблицу на время операции.
VACUUM (FULL, ANALYZE) marketplace_raw_payloads;
-- VACUUM (FULL, ANALYZE) dumping_runs;
-- VACUUM (FULL, ANALYZE) fast_dumping_jobs;
-- VACUUM (FULL, ANALYZE) product_test_jobs;

-- 4. Итоговый размер базы.
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
