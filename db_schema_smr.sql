-- ─────────────────────────────────────────────────────────────────
-- KTDB 수도권 스키마 (smr_*)
-- 적용 대상: ktdb DB (Azure Postgres geo-spatial-hub DEV 서버)
-- 적용 방법: psql -h ... -p 5432 -U ktdb -d ktdb -f db_schema_smr.sql
--
-- 출처: PRD docs/PRD_DB-플랫폼-Azure-전환.md §6
-- 데이터 사양: 1310 TAZ × 7년 (2023, 2025, 2030, 2035, 2040, 2045, 2050)
-- 7년 적재 추정: ~6.6GB (인덱스 포함 ~7.6GB)
-- ─────────────────────────────────────────────────────────────────

-- 안전: 기존 스키마 위에 재실행해도 idempotent
SET search_path TO public;

-- ============================================
-- 1. zones 마스터 (1310행, 모든 테이블의 FK)
-- ============================================
CREATE TABLE IF NOT EXISTS smr_zones (
  taz_seq    INT     PRIMARY KEY,        -- 1~1310 (존체계.xlsx '권역 존체계_읍면동')
  admin_code BIGINT  NOT NULL,           -- 행정기관코드_읍면동 (예: 1101072)
  sido       TEXT    NOT NULL,           -- 시도 (17개)
  sigu       TEXT,                        -- 시군구
  dong       TEXT,                        -- 행정동/리 (권역외부는 NULL)
  is_inner   SMALLINT NOT NULL CHECK (is_inner IN (1, 2))  -- 1=수도권 권역내부, 2=권역외부
);

CREATE INDEX IF NOT EXISTS idx_smr_zones_admin_code ON smr_zones(admin_code);
CREATE INDEX IF NOT EXISTS idx_smr_zones_sido_sigu  ON smr_zones(sido, sigu);
CREATE INDEX IF NOT EXISTS idx_smr_zones_inner      ON smr_zones(is_inner);

COMMENT ON TABLE  smr_zones IS '수도권 KTDB 1310 TAZ 마스터. 권역내부 1137 + 권역외부 173.';
COMMENT ON COLUMN smr_zones.taz_seq    IS 'TAZ 일련번호 1~1310 (모든 OD/socio FK)';
COMMENT ON COLUMN smr_zones.admin_code IS '행정기관코드 (예: 서울 종로구 청운효자동 = 1111051500)';
COMMENT ON COLUMN smr_zones.is_inner   IS '1=권역내부(수도권 1137존), 2=권역외부(나머지 173존)';


-- ============================================
-- 2. 사회경제지표 4종 (long format)
-- ============================================

-- 2.1 인구수: 1137 zones × 7 yrs × 202 (M/F × 0~100세) = ~1.6M 행
CREATE TABLE IF NOT EXISTS smr_socio_pop (
  year     SMALLINT NOT NULL,
  taz_seq  INT      NOT NULL REFERENCES smr_zones(taz_seq),
  gender   CHAR(1)  NOT NULL CHECK (gender IN ('M', 'F')),
  age      SMALLINT NOT NULL CHECK (age BETWEEN 0 AND 100),  -- 100 = 100세 이상
  value    REAL     NOT NULL,
  PRIMARY KEY (year, taz_seq, gender, age)
);
CREATE INDEX IF NOT EXISTS idx_smr_socio_pop_year_taz ON smr_socio_pop(year, taz_seq);

-- 2.2 취업자수: 인구와 동일 wide → long (~1.6M 행)
CREATE TABLE IF NOT EXISTS smr_socio_emp (
  year     SMALLINT NOT NULL,
  taz_seq  INT      NOT NULL REFERENCES smr_zones(taz_seq),
  gender   CHAR(1)  NOT NULL CHECK (gender IN ('M', 'F')),
  age      SMALLINT NOT NULL CHECK (age BETWEEN 0 AND 100),
  value    REAL     NOT NULL,
  PRIMARY KEY (year, taz_seq, gender, age)
);
CREATE INDEX IF NOT EXISTS idx_smr_socio_emp_year_taz ON smr_socio_emp(year, taz_seq);

-- 2.3 종사자수: (year, taz, indicator) — '3차'(3차산업) / '총'(전체)  ~16K 행
CREATE TABLE IF NOT EXISTS smr_socio_work (
  year      SMALLINT NOT NULL,
  taz_seq   INT      NOT NULL REFERENCES smr_zones(taz_seq),
  indicator CHAR(4)  NOT NULL CHECK (indicator IN ('3RD ', 'TOT ')),  -- 3차 / 총
  value     REAL     NOT NULL,
  PRIMARY KEY (year, taz_seq, indicator)
);
CREATE INDEX IF NOT EXISTS idx_smr_socio_work_year ON smr_socio_work(year);

-- 2.4 학생수: (year, taz, level) — 초/중/고/특수/대  ~40K 행
CREATE TABLE IF NOT EXISTS smr_socio_stu (
  year     SMALLINT NOT NULL,
  taz_seq  INT      NOT NULL REFERENCES smr_zones(taz_seq),
  level    CHAR(4)  NOT NULL CHECK (level IN ('ELEM', 'MID ', 'HIGH', 'SPEC', 'UNIV')),
  value    REAL     NOT NULL,
  PRIMARY KEY (year, taz_seq, level)
);
CREATE INDEX IF NOT EXISTS idx_smr_socio_stu_year ON smr_socio_stu(year);


-- ============================================
-- 3. OD 3종 (year 파티션, sparse-drop 적재)
-- ============================================

-- 3.1 목적OD: 5목적 (귀가/출근/등교/업무/기타). 90% nonzero, ~10.85M 행/7년
CREATE TABLE IF NOT EXISTS smr_od_purpose (
  year     SMALLINT NOT NULL,
  orgn_seq INT      NOT NULL,
  dest_seq INT      NOT NULL,
  home     REAL     NOT NULL DEFAULT 0,
  work     REAL     NOT NULL DEFAULT 0,
  scho     REAL     NOT NULL DEFAULT 0,
  busi     REAL     NOT NULL DEFAULT 0,
  othe     REAL     NOT NULL DEFAULT 0,
  PRIMARY KEY (year, orgn_seq, dest_seq)
) PARTITION BY LIST (year);

-- 3.2 주수단OD: 10수단. ~10.85M 행/7년
CREATE TABLE IF NOT EXISTS smr_od_main_mode (
  year      SMALLINT NOT NULL,
  orgn_seq  INT      NOT NULL,
  dest_seq  INT      NOT NULL,
  walk_bike REAL     NOT NULL DEFAULT 0,  -- 도보/자전거
  freight   REAL     NOT NULL DEFAULT 0,  -- 화물/기타
  etc_bus   REAL     NOT NULL DEFAULT 0,  -- 기타버스(시외/고속)
  rail      REAL     NOT NULL DEFAULT 0,  -- 일반철도
  ktx       REAL     NOT NULL DEFAULT 0,  -- KTX
  auto      REAL     NOT NULL DEFAULT 0,  -- 승용차
  taxi      REAL     NOT NULL DEFAULT 0,
  bus       REAL     NOT NULL DEFAULT 0,
  subw      REAL     NOT NULL DEFAULT 0,  -- 지하철
  bus_subw  REAL     NOT NULL DEFAULT 0,  -- 버스+지하철
  PRIMARY KEY (year, orgn_seq, dest_seq)
) PARTITION BY LIST (year);

-- 3.3 목적별주수단OD: 4목적 × 10수단. ~28.84M 행/7년 (40% sparse drop 적용)
CREATE TABLE IF NOT EXISTS smr_od_purpose_mode (
  year      SMALLINT NOT NULL,
  purpose   CHAR(4)  NOT NULL CHECK (purpose IN ('HOME', 'WORK', 'SCHO', 'OTHE')),
  orgn_seq  INT      NOT NULL,
  dest_seq  INT      NOT NULL,
  walk_bike REAL     NOT NULL DEFAULT 0,
  freight   REAL     NOT NULL DEFAULT 0,
  etc_bus   REAL     NOT NULL DEFAULT 0,
  rail      REAL     NOT NULL DEFAULT 0,
  ktx       REAL     NOT NULL DEFAULT 0,
  auto      REAL     NOT NULL DEFAULT 0,
  taxi      REAL     NOT NULL DEFAULT 0,
  bus       REAL     NOT NULL DEFAULT 0,
  subw      REAL     NOT NULL DEFAULT 0,
  bus_subw  REAL     NOT NULL DEFAULT 0,
  PRIMARY KEY (year, purpose, orgn_seq, dest_seq)
) PARTITION BY LIST (year);

-- ─────────────────────────────────────────────
-- 3.4 연도별 파티션 7개 (모든 OD 테이블 공통)
-- ─────────────────────────────────────────────
DO $$
DECLARE
  yr SMALLINT;
  parent TEXT;
  yrs SMALLINT[] := ARRAY[2023, 2025, 2030, 2035, 2040, 2045, 2050];
  parents TEXT[] := ARRAY['smr_od_purpose', 'smr_od_main_mode', 'smr_od_purpose_mode'];
BEGIN
  FOREACH parent IN ARRAY parents LOOP
    FOREACH yr IN ARRAY yrs LOOP
      EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES IN (%s)',
        parent || '_y' || yr, parent, yr
      );
    END LOOP;
  END LOOP;
END $$;

-- 자주 쓰는 인덱스 (orgn/dest 양방향 필터)
CREATE INDEX IF NOT EXISTS idx_smr_od_purpose_orgn       ON smr_od_purpose(orgn_seq);
CREATE INDEX IF NOT EXISTS idx_smr_od_purpose_dest       ON smr_od_purpose(dest_seq);
CREATE INDEX IF NOT EXISTS idx_smr_od_main_mode_orgn     ON smr_od_main_mode(orgn_seq);
CREATE INDEX IF NOT EXISTS idx_smr_od_main_mode_dest     ON smr_od_main_mode(dest_seq);
CREATE INDEX IF NOT EXISTS idx_smr_od_pm_orgn            ON smr_od_purpose_mode(orgn_seq);
CREATE INDEX IF NOT EXISTS idx_smr_od_pm_dest            ON smr_od_purpose_mode(dest_seq);
CREATE INDEX IF NOT EXISTS idx_smr_od_pm_purpose         ON smr_od_purpose_mode(purpose);


-- ============================================
-- 4. sync_log (동기화 메타)
-- ============================================
CREATE TABLE IF NOT EXISTS smr_sync_log (
  id          SERIAL    PRIMARY KEY,
  dataset     TEXT      NOT NULL,        -- 'zones' | 'socio_pop' | 'od_purpose' | ...
  year        SMALLINT,                   -- nullable (zones는 연도 없음)
  purpose     CHAR(4),                    -- nullable (od_purpose_mode 전용)
  rows_loaded INT       NOT NULL,
  duration_s  NUMERIC(10, 2) NOT NULL,
  source_md5  TEXT,                       -- 입력 zip/파일 md5 (idempotency)
  status      TEXT      NOT NULL DEFAULT 'OK',  -- 'OK' | 'FAIL'
  error_msg   TEXT,
  synced_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_smr_sync_log_synced_at ON smr_sync_log(synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_smr_sync_log_dataset   ON smr_sync_log(dataset, year);


-- ============================================
-- 5. COMMENT — 원본 ↔ DB 컬럼 매핑 (한글)
-- 출처: KTDB 수도권 OD 및 네트워크 설명자료 (2024년 수정 251020)
-- 매핑 마스터: docs/컬럼매핑표.md
-- ============================================

-- ── smr_zones (존체계.xlsx)
COMMENT ON COLUMN smr_zones.sido     IS '시도 (예: 서울특별시, 경기도)';
COMMENT ON COLUMN smr_zones.sigu     IS '시군구 (예: 종로구, 수원시 영통구)';
COMMENT ON COLUMN smr_zones.dong     IS '행정동/리 (권역외부=NULL)';

-- ── smr_socio_pop (SUB_POPYY.TXT — 인구수)
COMMENT ON TABLE  smr_socio_pop        IS '수도권 인구수 (SUB_POPYY.TXT 원본 wide 204컬럼을 long으로 정규화)';
COMMENT ON COLUMN smr_socio_pop.year   IS '연도 (2023, 2025, 2030, 2035, 2040, 2045, 2050)';
COMMENT ON COLUMN smr_socio_pop.gender IS '성별: M=남성, F=여성 (원본 헤더 "남0~100세 / 여 0~100세")';
COMMENT ON COLUMN smr_socio_pop.age    IS '연령 0~100세 (100=100세 이상). 원본 wide 컬럼 1세 단위';
COMMENT ON COLUMN smr_socio_pop.value  IS '인구 수 (명, 집체사가구 제외)';

-- ── smr_socio_emp (EMP_POP_YY.TXT — 취업자수)
COMMENT ON TABLE  smr_socio_emp        IS '수도권 취업자수 (EMP_POP_YY.TXT 원본 wide 204컬럼을 long으로 정규화)';
COMMENT ON COLUMN smr_socio_emp.year   IS '연도';
COMMENT ON COLUMN smr_socio_emp.gender IS '성별: M=남성, F=여성';
COMMENT ON COLUMN smr_socio_emp.age    IS '연령 0~100세 (100=100세 이상)';
COMMENT ON COLUMN smr_socio_emp.value  IS '취업자 수 (명)';

-- ── smr_socio_work (WORK_POPYY.TXT — 종사자수)
COMMENT ON TABLE  smr_socio_work           IS '수도권 종사자수 (WORK_POPYY.TXT). 원본 헤더 "3차 / 총종사자"';
COMMENT ON COLUMN smr_socio_work.indicator IS '지표: ''3RD ''=3차산업 종사자, ''TOT ''=총종사자';
COMMENT ON COLUMN smr_socio_work.value     IS '종사자 수 (명)';

-- ── smr_socio_stu (STU_POPYY.TXT — 학생수)
COMMENT ON TABLE  smr_socio_stu       IS '수도권 학생수 (STU_POPYY.TXT). 원본 헤더 "초 중 고 특수 대"';
COMMENT ON COLUMN smr_socio_stu.level IS '학교급: ELEM=초, MID =중, HIGH=고, SPEC=특수, UNIV=대학';
COMMENT ON COLUMN smr_socio_stu.value IS '학생 수 (명)';

-- ── smr_od_purpose (ODTRIPYY_F.OUT — 목적OD)
COMMENT ON TABLE  smr_od_purpose          IS '수도권 목적OD (ODTRIPYY_F.OUT). 5목적, 1310^2 OD pair × 7년, sparse drop 후 적재';
COMMENT ON COLUMN smr_od_purpose.year     IS '연도 (파티션 키)';
COMMENT ON COLUMN smr_od_purpose.orgn_seq IS '출발존 TAZ 일련번호 (1~1310, 원본 첫 컬럼)';
COMMENT ON COLUMN smr_od_purpose.dest_seq IS '도착존 TAZ 일련번호 (1~1310, 원본 셋째 컬럼)';
COMMENT ON COLUMN smr_od_purpose.home     IS '귀가 통행량 (원본 5번째 컬럼)';
COMMENT ON COLUMN smr_od_purpose.work     IS '출근 통행량';
COMMENT ON COLUMN smr_od_purpose.scho     IS '등교 통행량';
COMMENT ON COLUMN smr_od_purpose.busi     IS '업무 통행량';
COMMENT ON COLUMN smr_od_purpose.othe     IS '기타 통행량';

-- ── smr_od_main_mode (OD_MMODE_YY_F.TXT — 주수단OD)
COMMENT ON TABLE  smr_od_main_mode            IS '수도권 주수단OD (OD_MMODE_YY_F.TXT). 10수단, 1310^2 OD pair × 7년';
COMMENT ON COLUMN smr_od_main_mode.year       IS '연도 (파티션 키)';
COMMENT ON COLUMN smr_od_main_mode.orgn_seq   IS '출발존 TAZ 일련번호';
COMMENT ON COLUMN smr_od_main_mode.dest_seq   IS '도착존 TAZ 일련번호';
COMMENT ON COLUMN smr_od_main_mode.walk_bike  IS '도보/자전거';
COMMENT ON COLUMN smr_od_main_mode.freight    IS '화물/기타';
COMMENT ON COLUMN smr_od_main_mode.etc_bus    IS '기타버스(시외/고속버스 등)';
COMMENT ON COLUMN smr_od_main_mode.rail       IS '일반철도';
COMMENT ON COLUMN smr_od_main_mode.ktx        IS 'KTX 고속철도';
COMMENT ON COLUMN smr_od_main_mode.auto       IS '승용차';
COMMENT ON COLUMN smr_od_main_mode.taxi       IS '택시';
COMMENT ON COLUMN smr_od_main_mode.bus        IS '버스';
COMMENT ON COLUMN smr_od_main_mode.subw       IS '지하철';
COMMENT ON COLUMN smr_od_main_mode.bus_subw   IS '버스+지하철 연계';

-- ── smr_od_purpose_mode (OD_MMODE_<P>_YY_F.TXT — 목적별 주수단OD)
COMMENT ON TABLE  smr_od_purpose_mode            IS '수도권 목적별 주수단OD (OD_MMODE_HOME/WORK/SCHO/OTHE_YY_F.TXT). 4목적×10수단×7년. BUSI(업무)는 원본에 없음';
COMMENT ON COLUMN smr_od_purpose_mode.year       IS '연도 (파티션 키)';
COMMENT ON COLUMN smr_od_purpose_mode.purpose    IS '통행 목적: HOME=귀가, WORK=출근, SCHO=등교, OTHE=기타';
COMMENT ON COLUMN smr_od_purpose_mode.orgn_seq   IS '출발존 TAZ 일련번호';
COMMENT ON COLUMN smr_od_purpose_mode.dest_seq   IS '도착존 TAZ 일련번호';
COMMENT ON COLUMN smr_od_purpose_mode.walk_bike  IS '도보/자전거';
COMMENT ON COLUMN smr_od_purpose_mode.freight    IS '화물/기타';
COMMENT ON COLUMN smr_od_purpose_mode.etc_bus    IS '기타버스';
COMMENT ON COLUMN smr_od_purpose_mode.rail       IS '일반철도';
COMMENT ON COLUMN smr_od_purpose_mode.ktx        IS 'KTX';
COMMENT ON COLUMN smr_od_purpose_mode.auto       IS '승용차';
COMMENT ON COLUMN smr_od_purpose_mode.taxi       IS '택시';
COMMENT ON COLUMN smr_od_purpose_mode.bus        IS '버스';
COMMENT ON COLUMN smr_od_purpose_mode.subw       IS '지하철';
COMMENT ON COLUMN smr_od_purpose_mode.bus_subw   IS '버스+지하철 연계';

-- ── smr_sync_log
COMMENT ON TABLE  smr_sync_log              IS '데이터 동기화 메타. TRUNCATE+RELOAD 패턴, idempotent 검증용';
COMMENT ON COLUMN smr_sync_log.dataset      IS '데이터셋: zones / socio_pop / socio_emp / socio_work / socio_stu / od_purpose / od_main_mode / od_purpose_mode';
COMMENT ON COLUMN smr_sync_log.year         IS '연도 (zones는 NULL)';
COMMENT ON COLUMN smr_sync_log.purpose      IS '목적별주수단OD 전용 (HOME/WORK/SCHO/OTHE), 그 외 NULL';
COMMENT ON COLUMN smr_sync_log.rows_loaded  IS '실제 적재된 행 수 (sparse drop 후)';
COMMENT ON COLUMN smr_sync_log.duration_s   IS '적재 소요 시간 (초)';
COMMENT ON COLUMN smr_sync_log.source_md5   IS '입력 zip/원본 파일 md5 해시 (idempotency)';
COMMENT ON COLUMN smr_sync_log.status       IS 'OK | FAIL';


-- ============================================
-- 6. 검증 쿼리 (스키마 적용 후 실행)
-- ============================================
-- \dt+ smr_*           -- 모든 smr_* 테이블 + 크기 확인
-- \d+ smr_zones        -- zones 마스터 컬럼 + 인덱스
-- \d+ smr_od_purpose   -- 파티션 7개 (y2023, y2025, ...) 보임

-- 파티션 확인:
-- SELECT inhparent::regclass AS parent, inhrelid::regclass AS partition
-- FROM pg_inherits
-- WHERE inhparent::regclass::text LIKE 'smr_od_%'
-- ORDER BY parent, partition;
