-- Klado repeatable public-schema reinitialization.
--
-- WARNING: every execution deletes all tables, views, sequences and data in
-- the public schema, then recreates the slimmed application baseline.
-- Run with the application owner (normally `klado`):
--   psql -X -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/reinitialize_public.sql
--
-- Only platform tables are created: Data Center metadata (_import_registry,
-- _file_library, _qi_sheet_mappings, system_import_jobs),
-- app_settings. Runtime
-- tables owned by a feature module are created by that module, not here.

BEGIN;

DROP SCHEMA IF EXISTS public CASCADE;

CREATE SCHEMA public;
SET statement_timeout = 0;

SET lock_timeout = 0;

SET idle_in_transaction_session_timeout = 0;

SET client_encoding = 'UTF8';

SET standard_conforming_strings = on;

SET check_function_bodies = false;

SET xmloption = content;

SET client_min_messages = warning;

SET row_security = off;

--
-- Name: auth; Type: SCHEMA; Schema: -; Owner: -
--

--
-- Name: docs; Type: SCHEMA; Schema: -; Owner: -
--

--
-- Name: marketing; Type: SCHEMA; Schema: -; Owner: -
--

--
-- Name: reports; Type: SCHEMA; Schema: -; Owner: -
--

--
-- Name: research; Type: SCHEMA; Schema: -; Owner: -
--

--
-- Name: system; Type: SCHEMA; Schema: -; Owner: -
--

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: _file_library; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public._file_library (
    id integer NOT NULL,
    filename text NOT NULL,
    object_name text NOT NULL,
    size_bytes bigint DEFAULT 0,
    sheets_meta jsonb DEFAULT '[]'::jsonb,
    uploaded_at timestamp without time zone DEFAULT now()
);

--
-- Name: _file_library_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public._file_library_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: _file_library_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public._file_library_id_seq OWNED BY public._file_library.id;

--
-- Name: _import_registry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public._import_registry (
    id integer NOT NULL,
    table_name text NOT NULL,
    display_name text,
    target_db text DEFAULT 'pg'::text NOT NULL CONSTRAINT import_registry_public_target_only CHECK (target_db = 'pg'),
    fingerprint text NOT NULL,
    columns jsonb NOT NULL,
    row_count integer DEFAULT 0,
    source_file text,
    sheet_name text,
    modules text[] DEFAULT '{}'::text[],
    description text DEFAULT ''::text,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    update_count integer DEFAULT 0,
    source_file_path text,
    cleaning_rules jsonb
);

--
-- Name: _import_registry_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public._import_registry_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: _import_registry_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public._import_registry_id_seq OWNED BY public._import_registry.id;

--
-- Name: _qi_sheet_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public._qi_sheet_mappings (
    file_slug text NOT NULL,
    sheet_name text NOT NULL,
    table_name text NOT NULL,
    updated_at timestamp without time zone DEFAULT now(),
    mode text DEFAULT 'append'::text NOT NULL,
    cleaning_rules jsonb
);

--
-- Name: app_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_settings (
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: import_jobs; Type: TABLE; Schema: system; Owner: -
--

CREATE TABLE public.system_import_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    file_name character varying(500),
    module character varying(100),
    status character varying(50) DEFAULT 'pending'::character varying,
    rows_total integer,
    rows_ok integer,
    rows_error integer,
    error_detail jsonb,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone
);

--
-- Name: _file_library id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._file_library ALTER COLUMN id SET DEFAULT nextval('public._file_library_id_seq'::regclass);

--
-- Name: _import_registry id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._import_registry ALTER COLUMN id SET DEFAULT nextval('public._import_registry_id_seq'::regclass);

--
-- Name: _file_library _file_library_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._file_library
    ADD CONSTRAINT _file_library_pkey PRIMARY KEY (id);

--
-- Name: _import_registry _import_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._import_registry
    ADD CONSTRAINT _import_registry_pkey PRIMARY KEY (id);

--
-- Name: _import_registry _import_registry_table_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._import_registry
    ADD CONSTRAINT _import_registry_table_name_key UNIQUE (table_name);

--
-- Name: _qi_sheet_mappings _qi_sheet_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._qi_sheet_mappings
    ADD CONSTRAINT _qi_sheet_mappings_pkey PRIMARY KEY (file_slug, sheet_name);

--
-- Name: app_settings app_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_pkey PRIMARY KEY (key);

--
-- Name: import_jobs import_jobs_pkey; Type: CONSTRAINT; Schema: system; Owner: -
--

ALTER TABLE ONLY public.system_import_jobs
    ADD CONSTRAINT import_jobs_pkey PRIMARY KEY (id);

-- Current runtime schema not present in the original PostgreSQL dump.
-- Keep this file declarative: it must be safe on a blank database and safe to
-- rerun on an existing Klado database.

ALTER TABLE public._file_library
    ADD COLUMN IF NOT EXISTS share_token TEXT;

ALTER TABLE public._import_registry
    ADD COLUMN IF NOT EXISTS source_file_path TEXT,
    ADD COLUMN IF NOT EXISTS cleaning_rules JSONB;

GRANT USAGE, CREATE ON SCHEMA public TO klado;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO klado;

GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO klado;

COMMIT;
