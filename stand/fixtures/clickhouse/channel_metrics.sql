CREATE TABLE channel_metrics ENGINE = MergeTree ORDER BY (model, channel) AS
WITH channels AS (
    SELECT channel FROM spend
    UNION DISTINCT SELECT channel FROM events WHERE name IN ('impression', 'click')
    UNION DISTINCT SELECT 'unattributed' AS channel
), grid AS (
    SELECT channel, arrayJoin(['last_click', 'linear', 'time_decay']) AS model FROM channels
), credits AS (
    SELECT model, channel, sum(weight) AS conversions, sum(revenue_cents) AS revenue,
        sum(cost_cents) AS cost
    FROM attribution GROUP BY model, channel
), expenses AS (
    SELECT channel, sum(spend_cents) AS amount FROM spend GROUP BY channel
)
SELECT g.model AS model, g.channel AS channel, ifNull(c.conversions, 0.) AS conversions,
    ifNull(c.revenue, 0) AS revenue_cents, ifNull(c.cost, 0) AS cost_cents,
    ifNull(s.amount, 0) AS spend_cents, spend_cents / nullIf(conversions, 0.) AS cpa_cents,
    revenue_cents - cost_cents - spend_cents AS margin_cents
FROM grid AS g LEFT JOIN credits AS c ON c.model = g.model AND c.channel = g.channel
LEFT JOIN expenses AS s ON s.channel = g.channel;
