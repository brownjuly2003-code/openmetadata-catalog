CREATE TABLE events (
    event_id String, user_id String, at DateTime64(6, 'UTC'), name LowCardinality(String),
    channel String, campaign String, revenue_cents Int64, cost_cents Int64
) ENGINE = MergeTree ORDER BY (user_id, at, event_id);

CREATE TABLE spend (
    channel String, spend_cents Int64
) ENGINE = MergeTree ORDER BY channel;
