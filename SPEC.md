# 매크로 트래킹 사이트 — SPEC

4개국(US/KR/EA/JP) 금리와 경제지표를 자동 수집하고, FOMC 4사분면 예측을
기록·채점하는 개인 학습용 시스템.

> 이 문서는 Claude Code에 넘기는 사양서다. 구현 전에 "확인 필요" 항목을 먼저 해결할 것.

---

## 1. 목표

1. **금리 링크 분석** — 한국 국고채 금리가 UST / Bund / JGB 중 무엇에 연동되는지 정량화
2. **주차별 지표 트래킹** — 미국 주요 지표의 서프라이즈를 매파 스코어로 집계
3. **4사분면 예측 기록** — 회의별 확률 예측을 남기고 Brier 스코어로 사후 채점
4. **시장 대비 성과** — 내 예측 vs 시장 내재 확률을 같은 척도로 비교

핵심 원칙: **시장 견해는 입력이 아니라 경쟁자다.** 국채금리를 매파 스코어
입력에 넣으면 순환 참조가 되어 (a) 예측이 시장 복사가 되고 (b) 다른 지표의
설명력을 먹고 (c) Y축 사후 채점이 자기 자신을 맞히게 된다.

---

## 2. 4사분면 정의

```
                    매파 (Y+)
                       │
        매파적 인하 ●  │  ● 매파적 인상
                       │
   인하 ──────────────┼────────────── 인상  (X)
                       │
      비둘기적 인하 ●  │  ● 비둘기적 인상
                       │
                    비둘기 (Y-)
```

- **X축 = 실제 정책 액션.** 해당 회의의 기준금리 변화(bp)
- **Y축 = 시장 기대 대비 톤.** 시장에 이미 내재된 경로보다 매파적인가

두 축을 "인상/인하" × "매파/비둘기"로 잡으면 사실상 같은 축이라 사분면
2개가 빈다. 시장이 크게 움직이는 것은 오프대각선(hawkish cut, dovish hike)이다.

### 판정 규칙 (자동 채점용)

| 축 | 조건 | 라벨 |
|---|---|---|
| X | Δ정책금리 > 0 | 인상 |
| X | Δ정책금리 = 0 | 동결 |
| X | Δ정책금리 < 0 | 인하 |
| Y | 회의 직후 2Y 변화 > +5bp | 매파 |
| Y | −5bp ~ +5bp | 중립 |
| Y | < −5bp | 비둘기 |

Y축 판정에 "성명서 느낌" 대신 **회의 직후 2년물 반응 bp**를 쓰면 완전 자동
채점이 된다. 시장이 매파로 받았는지는 가격이 말해준다. 문구 해석(LLM 톤
스코어)은 별도 컬럼에 참고값으로만 병행한다.

---

## 3. 지표 마스터

`config/indicators.csv` — 64개. `etl/build_indicators.py` 로 생성되며
`hawkish_sign` / `weight` / `review_flag` 는 `etl/signs.py` 규칙에서 자동 산출.
**CSV의 부호를 손으로 고치지 말 것.** 규칙을 고치고 재생성한다.

### 컬럼

| 컬럼 | 설명 |
|---|---|
| `id` | 고유 id. 반드시 문자로 시작 (엑셀이 `2Y`를 날짜로 바꾸는 것 방지) |
| `category` | 부호 결정의 근거. `labor_level` / `labor_slack` 분리가 핵심 |
| `target_cb` | 어느 중앙은행의 판단 대상인가 (FED/BOK/ECB/BOJ/NONE) |
| `use_as` | 아래 4종 |
| `hawkish_sign` | +1 긴축 / −1 완화 / 0 집계제외 |
| `weight` | 1.0 초과는 연준이 명시 타깃하는 변수에만 |
| `review_flag` | `SLACK` / `ZERO` / `HIGHW` — 여기만 검토하면 됨 |
| `id_verified` | `N` 이면 첫 ETL 전 source_id 실측 확인 필요 |

### use_as

| 값 | 개수 | 용도 |
|---|---|---|
| `policy_input` | 43 | 매파 스코어 집계에 들어감 |
| `market_baseline` | 7 | 시장 컨센서스. 내 예측의 경쟁자 |
| `kr_link_input` | 10 | 한국 금리 링크 회귀의 설명변수 |
| `outcome` | 4 | 결과 변수. 사후 채점용 |

### 매파 스코어 산식

```
score = Σ(surprise_z × hawkish_sign × weight) / Σ|weight|
```

가중치 합으로 나눠 z-score 스케일을 유지한다. 지표 수가 달라져도 비교 가능.

**중요: 4사분면 스코어는 `target_cb = FED` 且 `use_as = policy_input` 인
것만 집계한다.** 한국/유로존/일본 지표는 각자의 중앙은행 블록에서 별도 집계.
섞으면 의미 없는 숫자가 나온다.

---

## 4. 데이터 스키마

```sql
-- 발표 지표 (리비전 추적)
CREATE TABLE observations (
  indicator_id TEXT,
  ref_period   DATE,          -- 지표 기준 시점
  release_ts   TIMESTAMPTZ,   -- 발표 시각. UTC 저장, KST 렌더
  vintage_date DATE,          -- 리비전 추적. 나중에 추가하면 과거를 못 살림
  actual       NUMERIC,
  consensus    NUMERIC,
  previous     NUMERIC,
  surprise_z   NUMERIC,
  PRIMARY KEY (indicator_id, ref_period, vintage_date)
);

-- 일별 금리/가격
CREATE TABLE series_daily (
  series_id TEXT, date DATE, value NUMERIC, chg_bp NUMERIC,
  PRIMARY KEY (series_id, date)
);

-- 내 예측
CREATE TABLE forecasts (
  id SERIAL PRIMARY KEY,
  meeting_date DATE, forecast_date DATE,
  q_hike_hawk NUMERIC, q_hike_dove NUMERIC,
  q_cut_hawk  NUMERIC, q_cut_dove  NUMERIC,   -- 합 100
  rationale TEXT,
  key_indicators TEXT[],
  position_plan JSONB,
  actual_quadrant TEXT, brier_score NUMERIC
);

-- 시장 = 경쟁자
CREATE TABLE market_baseline (
  meeting_date DATE,
  snapshot_date DATE,          -- 궤적이 남아야 "그때 시장은?"을 볼 수 있음
  hike_prob NUMERIC, hold_prob NUMERIC, cut_prob NUMERIC,
  ust2y NUMERIC,
  implied_hawkish_z NUMERIC,
  brier_score NUMERIC,
  PRIMARY KEY (meeting_date, snapshot_date)
);
```

**시각은 전부 UTC 저장, 렌더링 시에만 KST 변환.** 미국 서머타임 때문에
발표시각이 KST 기준 1시간씩 밀린다. 로컬 시각으로 저장하면 캘린더가 깨진다.

### 채점

```
Brier = Σ(p_i − o_i)² / N        (o_i: 실제 발생 1, 아니면 0)
```

6개월 쌓이면 캘리브레이션 곡선으로 본인 편향(예: 매파 과대평가)이 보인다.
**누적 Brier(나) − Brier(시장) 이 어느 국면에서 양수인지**가 핵심 지표다.
대부분 구간에서 시장이 이기는 게 정상이며, 전환점 근처에서 이겼는지를 본다.

---

## 5. 한국 금리 링크 분석

```
Δ국고3Y(bp) ~ β₁·ΔUST2Y + β₂·ΔBund2Y + β₃·ΔJGB10Y + β₄·ΔUSDKRW + ε
```

산출물: 롤링 60일/120일 β와 상관계수, lead-lag 교차상관(한국이 며칠 후행하는지),
시기별 β 변화 추이(긴축기 vs 현재).

---

## 6. 자동화

```
GitHub Actions (평일 UTC 22:00 = KST 익일 07:00)
  └─ etl/fetch_*.py       → FRED / ECOS / ECB
  └─ etl/compute.py       → surprise_z, 롤링 β, 매파 스코어
  └─ git commit & push    → 배포 트리거
```

- ETL은 **개별 소스 실패를 허용**하고 실패 로그를 남긴다. 크롤링 소스는 반드시 깨진다
- `assert len(rows) > 0` 을 넣어 **조용한 실패**(에러 없이 빈 데이터)를 잡는다
- FOMC 당일만: `claude -p` 로 성명서 톤 스코어 산출 → 참고 컬럼에 저장

---

## 7. 구현 전 확인 필요

| # | 항목 | 비고 |
|---|---|---|
| 1 | `id_verified = N` 인 34개 source_id 실측 | ECOS/ECB/e-Stat/BOJ 코드. **FRED 30개는 확인됨** |
| 2 | 컨센서스 조달 경로 | 무료 API 없음. 과거 백필 포기하고 오늘부터 수집 권장 |
| 3 | ISM / S&P PMI | 라이선스 문제로 FRED 미제공. 수동 또는 크롤링 |
| 4 | BDI / SCFI | 무료 API 사실상 없음. 실패 허용 설계 필수 |
| 5 | `review_flag` 10건 검토 | 아래 |

### review_flag 10건

- `HIGHW` (4) — weight 1.0 초과: us_core_pce 1.5, us_core_cpi 1.3, us_nfp 1.3, us_umich_inflexp 1.1
- `SLACK` (6) — 부호가 뒤집히는 자리: us_unrate, us_iclaims, us_cclaims, us_u6, kr_unrate, ea_unrate

---

## 8. MVP 순서

| 주차 | 범위 |
|---|---|
| 1 | FRED + ECOS 연결. 4개국 정책금리/국채금리 차트 1페이지. **여기서 스코프를 끊을 것** |
| 2 | 롤링 상관/회귀 → "한국이 어디에 링크됐나" 페이지 |
| 3 | 경제 캘린더 + surprise z-score |
| 4 | 4사분면 입력 폼 + Brier 스코어보드 (나 vs 시장) |
| 이후 | BDI 등 크롤링 소스, LLM 톤 스코어 |

스택: Next.js + Postgres(또는 SQLite) + Python ETL(GitHub Actions).

---

## 9. 자동화하지 않는 것

- **4사분면 확률 입력** — 직접 찍는 게 이 프로젝트의 본체다
- **포지션 매핑 매트릭스** — 시스템은 빈 매트릭스와 사후 실제 자산 수익률만
  제공하고, 가설은 본인이 채운다. "내 가설 vs 실제" 비교가 목적
