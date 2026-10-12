-- IntelliHire MVP schema (run in the Supabase SQL editor).
-- Raw layer is append-only: parsers can be re-run any time without re-scraping.

create extension if not exists vector;

create table if not exists raw_postings (
    id            bigserial primary key,
    source        text        not null,
    source_id     text        not null,
    url           text        not null,
    fetched_at    timestamptz not null default now(),
    content_hash  text        not null,
    html          text        not null,
    unique (source, source_id, content_hash)
);

create table if not exists postings (
    id              bigserial primary key,
    source          text        not null,
    source_id       text        not null,
    url             text        not null,
    title           text,
    organization    text,
    city            text,
    posted_date     date,
    -- Phone numbers are personal data: store only a salted hash (useful as a
    -- fraud feature: "same number on 40 different ads") and a masked display form.
    apply_phone_hash   text,
    apply_phone_masked text,
    image_url       text,
    ocr_text        text,
    ocr_engine      text,
    -- text-based sources (e.g. Rozee) provide these directly; image-based ones get them via OCR later
    deadline        date,
    experience      text,
    description     text,
    skills          text,
    work_mode       text,
    salary          text,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    unique (source, source_id)
);

create index if not exists postings_city_idx    on postings (city);
create index if not exists postings_posted_idx  on postings (posted_date desc);
create index if not exists postings_phone_idx   on postings (apply_phone_hash);

create table if not exists scrape_runs (
    id             bigserial primary key,
    source         text        not null,
    started_at     timestamptz not null default now(),
    finished_at    timestamptz,
    status         text        not null default 'running',  -- running | ok | error | locked
    discovered     integer     not null default 0,
    new_postings   integer     not null default 0,
    errors         integer     not null default 0,
    newest_posted  date,
    note           text
);

-- Ads judged outside the active window (too old / undated / future-dated): remembered so they
-- are never fetched again. No HTML is kept for these.
create table if not exists skipped_postings (
    id           bigserial primary key,
    source       text        not null,
    source_id    text        not null,
    reason       text        not null,   -- too_old | no_date | future_date | wrong_year | expired
    posted_date  date,
    skipped_at   timestamptz not null default now(),
    unique (source, source_id)
);

-- Server-side only: the API uses the service connection string.
alter table raw_postings     enable row level security;
alter table postings         enable row level security;
alter table scrape_runs      enable row level security;
alter table skipped_postings enable row level security;
