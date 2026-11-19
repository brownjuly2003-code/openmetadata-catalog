CREATE TABLE user_funnel ENGINE = MergeTree ORDER BY step_number AS
WITH histories AS (
    SELECT minIf(at, name = 'impression') AS first_view,
        arraySort(groupArrayIf(at, name = 'click')) AS clicks,
        arraySort(groupArrayIf(at, name = 'install')) AS installs,
        arraySort(groupArrayIf(at, name = 'registration')) AS registrations,
        arraySort(groupArrayIf(at, name = 'subscription')) AS subscriptions
    FROM events GROUP BY user_id HAVING countIf(name = 'impression') > 0
), reached AS (
    -- With no maximum duration, the earliest valid step allows every later continuation.
    SELECT arrayFirstOrNull(t -> t > first_view, clicks) AS click_at,
        arrayFirstOrNull(t -> ifNull(t > click_at, 0), installs) AS install_at,
        arrayFirstOrNull(t -> ifNull(t > install_at, 0), registrations) AS register_at,
        arrayFirstOrNull(t -> ifNull(t > register_at, 0), subscriptions) AS subscribe_at
    FROM histories
), totals AS (
    SELECT count() AS viewed, count(click_at) AS clicked, count(install_at) AS installed,
        count(register_at) AS registered, count(subscribe_at) AS subscribed
    FROM reached
)
SELECT stage.1 AS step_number, stage.2 AS step, stage.3 AS users
FROM totals ARRAY JOIN [(1, 'impression', viewed), (2, 'click', clicked), (3, 'install', installed),
    (4, 'registration', registered), (5, 'subscription', subscribed)] AS stage;
