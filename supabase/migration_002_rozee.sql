-- Run once in the Supabase SQL editor (existing data is kept; all new columns are nullable).
alter table postings add column if not exists deadline    date;
alter table postings add column if not exists experience  text;
alter table postings add column if not exists description text;
alter table postings add column if not exists skills      text;
alter table postings add column if not exists work_mode   text;
alter table postings add column if not exists salary      text;

create index if not exists postings_deadline_idx on postings (deadline);
create index if not exists postings_source_idx   on postings (source);

-- skipped_postings (added in the freshness update); safe to run again.
create table if not exists skipped_postings (
    id           bigserial primary key,
    source       text        not null,
    source_id    text        not null,
    reason       text        not null,   -- too_old | no_date | future_date | wrong_year | expired
    posted_date  date,
    skipped_at   timestamptz not null default now(),
    unique (source, source_id)
);
alter table skipped_postings enable row level security;
