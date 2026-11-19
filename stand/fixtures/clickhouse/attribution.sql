CREATE TABLE attribution ENGINE = MergeTree ORDER BY (model, converted_at, conversion_id, channel) AS
WITH histories AS (
    SELECT user_id,
        arraySort(groupArrayIf((at, event_id, channel), name = 'click')) AS clicks,
        arraySort(groupArrayIf((at, event_id, channel), name = 'impression')) AS impressions
    FROM events WHERE name IN ('click', 'impression') GROUP BY user_id
), paths AS (
    SELECT e.event_id AS conversion_id, e.at AS converted_at,
        e.revenue_cents AS original_revenue, e.cost_cents AS original_cost,
        arrayFilter(t -> t.1 >= e.at - INTERVAL 7 DAY AND t.1 < e.at, h.clicks) AS recent_clicks,
        arrayFilter(t -> t.1 >= e.at - INTERVAL 1 DAY AND t.1 < e.at, h.impressions) AS recent_views,
        multiIf(notEmpty(recent_clicks), recent_clicks, notEmpty(recent_views), recent_views,
            [(e.at, '', 'unattributed')]) AS path
    FROM events AS e LEFT JOIN histories AS h ON h.user_id = e.user_id
    WHERE e.name = 'subscription'
), touches AS (
    SELECT conversion_id, converted_at, original_revenue, original_cost, touch.3 AS channel,
        touch.2 = path[-1].2 AS is_last, length(path) AS touch_count,
        exp2(-dateDiff('microsecond', touch.1, converted_at) / 86400000000.) AS decay
    FROM paths ARRAY JOIN path AS touch
), normalized AS (
    SELECT *, decay / sum(decay) OVER (PARTITION BY conversion_id) AS decay_weight
    FROM touches
), channel_weights AS (
    SELECT conversion_id, converted_at, original_revenue, original_cost, model, channel,
        sum(multiIf(model = 'last_click', toFloat64(is_last), model = 'linear',
            1.0 / touch_count, decay_weight)) AS weight
    FROM normalized ARRAY JOIN ['last_click', 'linear', 'time_decay'] AS model
    GROUP BY conversion_id, converted_at, original_revenue, original_cost, model, channel
    HAVING weight > 0
), quotas AS (
    SELECT *, original_revenue * weight AS revenue_quota, original_cost * weight AS cost_quota,
        toInt64(floor(revenue_quota)) AS revenue_floor, toInt64(floor(cost_quota)) AS cost_floor
    FROM channel_weights
)
SELECT conversion_id, converted_at, model, channel, weight,
    revenue_floor + (row_number() OVER (
        PARTITION BY conversion_id, model ORDER BY revenue_quota - revenue_floor DESC, channel
    ) <= original_revenue - sum(revenue_floor) OVER per_model) AS revenue_cents,
    cost_floor + (row_number() OVER (
        PARTITION BY conversion_id, model ORDER BY cost_quota - cost_floor DESC, channel
    ) <= original_cost - sum(cost_floor) OVER per_model) AS cost_cents
FROM quotas WINDOW per_model AS (PARTITION BY conversion_id, model);
