-- A keyword is either local (searched with the customer's city) or nationwide (searched as it stands).
-- Input: migrations 001-018. | Output: column keywords.scope. Guarded, so running the file twice changes nothing.

alter table public.keywords add column if not exists scope text not null default 'local';
alter table public.keywords drop constraint if exists keywords_scope_check;
alter table public.keywords add constraint keywords_scope_check check (scope in ('local', 'nationwide'));