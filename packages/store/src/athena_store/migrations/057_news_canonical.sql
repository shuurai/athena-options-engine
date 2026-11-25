-- V2-P3 (Q5 / Q6 / §6.1 / §6.3 N1). Additive canonical news read model.
-- news_events is NOT modified and is NOT dropped. The legacy map keeps one
-- immutable row per legacy fingerprint and is not part of the association key.
CREATE TABLE news_articles (
    article_id text PRIMARY KEY,
    identity_version text NOT NULL,
    identity_hash text NOT NULL,
    normalized_title text NOT NULL,
    canonical_title text NOT NULL,
    published_day date NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (identity_version, identity_hash)
);

CREATE TABLE news_article_symbols (
    association_id text PRIMARY KEY,
    article_id text NOT NULL REFERENCES news_articles(article_id),
    symbol text NOT NULL,
    source text NOT NULL DEFAULT '',
    url text NOT NULL DEFAULT '',
    event_type text NOT NULL DEFAULT '',
    sentiment numeric NOT NULL DEFAULT 0,
    dismissed boolean NOT NULL DEFAULT false,
    effective_ts timestamptz NOT NULL,
    day_precision text NOT NULL DEFAULT 'provider',
    original_ts timestamptz,
    ts timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (article_id, symbol)
);

CREATE TABLE news_legacy_event_map (
    legacy_fingerprint text NOT NULL,
    article_id text NOT NULL REFERENCES news_articles(article_id),
    association_id text NOT NULL REFERENCES news_article_symbols(association_id),
    legacy_ts timestamptz NOT NULL,
    legacy_write_status text NOT NULL DEFAULT 'inserted',
    source text NOT NULL DEFAULT '',
    url text NOT NULL DEFAULT '',
    event_type text NOT NULL DEFAULT '',
    sentiment numeric NOT NULL DEFAULT 0,
    title text NOT NULL DEFAULT '',
    symbol text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (legacy_fingerprint, article_id)
);

CREATE INDEX news_articles_day ON news_articles (published_day, article_id);
CREATE INDEX news_article_symbols_symbol ON news_article_symbols (symbol, effective_ts DESC);
CREATE INDEX news_legacy_event_map_assoc ON news_legacy_event_map (association_id);
CREATE INDEX news_legacy_event_map_fp ON news_legacy_event_map (legacy_fingerprint);
