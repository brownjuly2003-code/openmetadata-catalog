-- PostgreSQL schema for the synthetic orders; applied by `python -m order_picking.stand load`.
-- Idempotent. Constraints repeat the input contract from docs/METRICS.md.
CREATE SCHEMA IF NOT EXISTS picking;

CREATE TABLE IF NOT EXISTS picking.orders (
    order_id    text PRIMARY KEY,
    branch      text NOT NULL CHECK (branch <> ''),
    picker      text NOT NULL,
    created_at  timestamptz NOT NULL,
    started_at  timestamptz,
    finished_at timestamptz,
    status      text NOT NULL CHECK (status IN ('completed', 'pending', 'cancelled')),
    requested   integer NOT NULL CHECK (requested > 0),
    picked      integer NOT NULL CHECK (picked >= 0 AND picked <= requested),
    errors      integer NOT NULL CHECK (errors >= 0 AND errors <= picked),
    CONSTRAINT completed_has_interval CHECK (
        status <> 'completed'
        OR (picker <> '' AND started_at IS NOT NULL AND finished_at IS NOT NULL
            AND created_at <= started_at AND started_at < finished_at)
    ),
    CONSTRAINT open_orders_are_unstarted CHECK (
        status = 'completed'
        OR (started_at IS NULL AND finished_at IS NULL AND picked = 0 AND errors = 0)
    )
);

-- One row per order with the derived fields every card uses. created_day is the UTC
-- calendar day of creation, as in the offline report.
CREATE OR REPLACE VIEW picking.order_facts AS
SELECT order_id,
       branch,
       picker,
       status,
       requested,
       picked,
       errors,
       (created_at AT TIME ZONE 'UTC')::date AS created_day,
       extract(epoch FROM finished_at - started_at)::double precision AS pick_seconds,
       extract(epoch FROM started_at - created_at)::double precision AS wait_seconds
FROM picking.orders;
