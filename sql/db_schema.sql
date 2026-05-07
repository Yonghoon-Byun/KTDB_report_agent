-- KTDB 통합 분석 에이전트 DB 스키마 (Supabase Postgres)
-- 시트 master 가정 / 동기화 스크립트가 truncate-and-reload 방식으로 갱신

DROP TABLE IF EXISTS socio CASCADE;
DROP TABLE IF EXISTS od_purpose CASCADE;
DROP TABLE IF EXISTS od_main_mode CASCADE;
DROP TABLE IF EXISTS od_access_mode CASCADE;
DROP TABLE IF EXISTS zones CASCADE;

-- ─────────────────────────────────────────────────────────────
-- ZONE 마스터: 시도·시군구·존번호 매핑
-- ─────────────────────────────────────────────────────────────
CREATE TABLE zones (
    zone     INTEGER PRIMARY KEY,
    sido     TEXT NOT NULL,
    sigu     TEXT NOT NULL
);
CREATE INDEX idx_zones_sido ON zones(sido);
CREATE INDEX idx_zones_sigu ON zones(sigu);

-- ─────────────────────────────────────────────────────────────
-- 사회경제지표 (long format): 지표 6종 × 존 × 연도(2023~2050)
--   indicator_code: POP_TOT, POP_YNG, POP_15P, EMP, STU, WORK_TOT
-- ─────────────────────────────────────────────────────────────
CREATE TABLE socio (
    indicator_code TEXT NOT NULL,
    zone           INTEGER NOT NULL REFERENCES zones(zone),
    year           INTEGER NOT NULL,
    value          DOUBLE PRECISION,
    PRIMARY KEY (indicator_code, zone, year)
);
CREATE INDEX idx_socio_indicator_year ON socio(indicator_code, year);
CREATE INDEX idx_socio_zone ON socio(zone);

-- ─────────────────────────────────────────────────────────────
-- 목적OD: 연도별 데이터를 단일 테이블에 year 컬럼으로 통합
-- ─────────────────────────────────────────────────────────────
CREATE TABLE od_purpose (
    year  INTEGER NOT NULL,
    orgn  INTEGER NOT NULL,
    dest  INTEGER NOT NULL,
    work  DOUBLE PRECISION,
    scho  DOUBLE PRECISION,
    busi  DOUBLE PRECISION,
    home  DOUBLE PRECISION,
    othe  DOUBLE PRECISION,
    PRIMARY KEY (year, orgn, dest)
);
CREATE INDEX idx_od_purpose_orgn ON od_purpose(orgn);
CREATE INDEX idx_od_purpose_year ON od_purpose(year);

-- ─────────────────────────────────────────────────────────────
-- 주수단OD: 연도별 데이터 통합
-- ─────────────────────────────────────────────────────────────
CREATE TABLE od_main_mode (
    year  INTEGER NOT NULL,
    orgn  INTEGER NOT NULL,
    dest  INTEGER NOT NULL,
    auto  DOUBLE PRECISION,
    obus  DOUBLE PRECISION,
    subw  DOUBLE PRECISION,
    rail  DOUBLE PRECISION,
    erai  DOUBLE PRECISION,
    PRIMARY KEY (year, orgn, dest)
);
CREATE INDEX idx_od_main_mode_orgn ON od_main_mode(orgn);
CREATE INDEX idx_od_main_mode_year ON od_main_mode(year);

-- ─────────────────────────────────────────────────────────────
-- 접근수단OD: 2023년만 (단일 탭)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE od_access_mode (
    year     INTEGER NOT NULL,
    orgn     INTEGER NOT NULL,
    dest     INTEGER NOT NULL,
    att_aant DOUBLE PRECISION,
    att_obus DOUBLE PRECISION,
    PRIMARY KEY (year, orgn, dest)
);
CREATE INDEX idx_od_access_mode_orgn ON od_access_mode(orgn);

-- ─────────────────────────────────────────────────────────────
-- 동기화 메타데이터: 마지막 동기화 시각 추적
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_log (
    id           SERIAL PRIMARY KEY,
    table_name   TEXT NOT NULL,
    rows_loaded  INTEGER,
    duration_s   DOUBLE PRECISION,
    synced_at    TIMESTAMPTZ DEFAULT NOW()
);
