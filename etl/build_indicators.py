"""
indicators.csv 생성기.

지표 목록(INDICATORS)만 수정하면 hawkish_sign / weight / review_flag 는
signs.py 규칙에 따라 자동으로 다시 계산된다. 부호를 손으로 고치지 말 것.

컬럼
----
id            고유 id. 반드시 문자로 시작 (엑셀이 2Y 를 날짜로 바꾸는 것 방지)
item_code     source_id 하나가 여러 시계열을 담는 통계표(예: ECOS)일 때의
              하위 항목코드. 그 외 소스는 빈 문자열
              ECOS는 ITEM_CODE1~4까지 계층을 가질 수 있어, 2단계 이상 필요하면
              "/"로 이어붙인다 (예: kr_unrate "I61BC/I28B" = 실업률/계절조정)
target_cb     이 지표가 어느 중앙은행의 판단 대상인가 (FED/BOK/ECB/BOJ/NONE)
use_as        policy_input   -> 매파 스코어 집계에 들어감
              market_baseline-> 시장 컨센서스. 내 예측의 '경쟁자'
              kr_link_input  -> 한국 금리 링크 회귀의 설명변수
              outcome        -> 결과 변수. 사후 채점용
id_verified   source_id(+item_code) 를 실제로 확인했는지. N 이면 첫 ETL 실행 전 확인 필요
"""

import csv
from signs import resolve_sign, resolve_weight, needs_review

# (id, name_ko, country, category, source, source_id, item_code, freq,
#  transform, target_cb, use_as, id_verified)
INDICATORS = [
    # ============================================================ 미국 — 물가
    ("us_core_pce",  "근원 PCE 물가",      "US", "inflation",     "fred", "PCEPILFE",   "", "M", "yoy",        "FED", "policy_input", "Y"),
    ("us_pce",       "PCE 물가",           "US", "inflation",     "fred", "PCEPI",      "", "M", "yoy",        "FED", "policy_input", "Y"),
    ("us_core_cpi",  "근원 CPI",           "US", "inflation",     "fred", "CPILFESL",   "", "M", "yoy",        "FED", "policy_input", "Y"),
    ("us_cpi",       "CPI",                "US", "inflation",     "fred", "CPIAUCSL",   "", "M", "yoy",        "FED", "policy_input", "Y"),
    ("us_ppi",       "PPI 최종수요",       "US", "inflation",     "fred", "PPIFIS",     "", "M", "yoy",        "FED", "policy_input", "Y"),
    ("us_umich_inflexp", "미시간 1년 기대인플레", "US", "inflation_exp", "fred", "MICH", "", "M", "level",     "FED", "policy_input", "Y"),

    # ==================================================== 미국 — 노동(수준)
    ("us_nfp",       "비농업고용",         "US", "labor_level",   "fred", "PAYEMS",         "", "M", "mom_diff", "FED", "policy_input", "Y"),
    ("us_ahe",       "시간당 평균임금",    "US", "labor_level",   "fred", "CES0500000003",  "", "M", "yoy",      "FED", "policy_input", "Y"),
    ("us_awh",       "주당 평균 근로시간", "US", "labor_level",   "fred", "AWHAETP",        "", "M", "level",    "FED", "policy_input", "Y"),
    ("us_jolts",     "JOLTS 구인건수",     "US", "labor_level",   "fred", "JTSJOL",         "", "M", "level",    "FED", "policy_input", "Y"),
    ("us_lfpr",      "경제활동참가율",     "US", "labor_level",   "fred", "CIVPART",        "", "M", "level",    "FED", "policy_input", "Y"),

    # ==================================================== 미국 — 노동(유휴)
    ("us_unrate",    "실업률",             "US", "labor_slack",   "fred", "UNRATE", "", "M", "level",      "FED", "policy_input", "Y"),
    ("us_iclaims",   "신규 실업수당청구",  "US", "labor_slack",   "fred", "ICSA",   "", "W", "level_4wma", "FED", "policy_input", "Y"),
    ("us_cclaims",   "연속 실업수당청구",  "US", "labor_slack",   "fred", "CCSA",   "", "W", "level",      "FED", "policy_input", "Y"),
    ("us_u6",        "U-6 광의실업률",     "US", "labor_slack",   "fred", "U6RATE", "", "M", "level",      "FED", "policy_input", "Y"),

    # ======================================================= 미국 — 성장/수요
    ("us_ism_mfg",   "ISM 제조업 PMI",     "US", "growth", "manual", "ISM_MFG_PMI", "", "M", "level",   "FED", "policy_input", "N"),
    ("us_ism_svc",   "ISM 서비스업 PMI",   "US", "growth", "manual", "ISM_SVC_PMI", "", "M", "level",   "FED", "policy_input", "N"),
    ("us_retail_ctl","소매판매 control",   "US", "growth", "census", "MARTS_CTL",   "", "M", "mom_pct", "FED", "policy_input", "N"),
    ("us_durable",   "내구재 주문",        "US", "growth", "fred",   "DGORDER",     "", "M", "mom_pct", "FED", "policy_input", "Y"),
    ("us_indpro",    "산업생산",           "US", "growth", "fred",   "INDPRO",      "", "M", "mom_pct", "FED", "policy_input", "Y"),
    ("us_gdp_adv",   "실질GDP 속보치",     "US", "growth", "fred",   "GDPC1",       "", "Q", "qoq_saar","FED", "policy_input", "Y"),

    # ========================================================= 미국 — 금융/정책
    ("us_fftr_upper","연준 목표금리 상단", "US", "policy",    "fred", "DFEDTARU",     "", "D", "level",  "FED", "outcome",         "Y"),
    ("us_effr",      "실효 연방기금금리",  "US", "policy",    "fred", "EFFR",         "", "D", "level",  "FED", "outcome",         "Y"),
    ("us_2y",        "미 국채 2년",        "US", "financial", "fred", "DGS2",         "", "D", "level",  "FED", "market_baseline", "Y"),
    ("us_10y",       "미 국채 10년",       "US", "financial", "fred", "DGS10",        "", "D", "level",  "FED", "market_baseline", "Y"),
    ("us_2s10s",     "2s10s 스프레드",     "US", "financial", "fred", "T10Y2Y",       "", "D", "level",  "FED", "market_baseline", "Y"),
    ("us_hy_oas",    "하이일드 OAS",       "US", "financial", "fred", "BAMLH0A0HYM2", "", "D", "level",  "FED", "market_baseline", "Y"),
    ("us_nfci",      "시카고연은 금융환경","US", "financial", "fred", "NFCI",         "", "W", "level",  "FED", "market_baseline", "Y"),

    # ================================================================== 한국
    ("kr_cpi",          "한국 CPI",            "KR", "inflation",   "ecos", "901Y009", "0",         "M", "yoy",     "BOK", "policy_input",  "Y"),
    ("kr_core_cpi",     "한국 근원CPI",        "KR", "inflation",   "ecos", "901Y010", "QB",        "M", "yoy",     "BOK", "policy_input",  "Y"),
    ("kr_export",       "수출(월간)",          "KR", "external",    "ecos", "403Y001", "*AA",       "M", "yoy",     "BOK", "policy_input",  "Y"),
    ("kr_export_20d",   "관세청 1~20일 수출",  "KR", "external",    "customs", "EXPORT_20D", "", "M", "yoy", "BOK", "policy_input", "N"),
    ("kr_export_semi",  "반도체 수출",         "KR", "external",    "customs", "EXPORT_SEMI","", "M", "yoy", "BOK", "policy_input", "N"),
    ("kr_ip",           "산업생산",            "KR", "growth",      "kosis", "IP_SA",   "", "M", "mom_pct", "BOK", "policy_input",  "N"),
    ("kr_unrate",       "한국 실업률",         "KR", "labor_slack", "ecos", "901Y027", "I61BC/I28B","M", "level",   "BOK", "policy_input",  "Y"),
    ("kr_current_acct", "경상수지",            "KR", "external",    "ecos", "301Y017", "SA000",     "M", "level",   "BOK", "policy_input",  "Y"),
    ("kr_household_debt","가계신용 증감",      "KR", "credit"   ,   "ecos", "151Y001", "1000000",   "Q", "yoy",     "BOK", "policy_input",  "Y"),
    ("kr_house_price",  "주택매매가격지수",    "KR", "credit"   ,   "ecos", "901Y062", "P63A",      "M", "mom_pct", "BOK", "policy_input",  "Y"),
    ("kr_base_rate",    "한은 기준금리",       "KR", "policy",      "ecos", "722Y001", "0101000",   "D", "level",   "BOK", "outcome",       "Y"),
    ("kr_ktb3y",        "국고채 3년",          "KR", "financial",   "ecos", "817Y002", "010200000", "D", "level",   "BOK", "kr_link_input", "Y"),
    ("kr_ktb10y",       "국고채 10년",         "KR", "financial",   "ecos", "817Y002", "010210000", "D", "level",   "BOK", "kr_link_input", "Y"),

    # ================================================================ 유로존
    ("ea_hicp",      "HICP",             "EA", "inflation", "ecb",  "ICP.M.U2.N.000000.4.ANR",             "", "M", "level", "ECB", "policy_input",  "N"),
    ("ea_core_hicp", "근원 HICP",        "EA", "inflation", "ecb",  "ICP.M.U2.N.XEF000.4.ANR",             "", "M", "level", "ECB", "policy_input",  "N"),
    ("ea_wages",     "협상임금",         "EA", "labor_level","ecb", "ECB_NEGOTIATED_WAGES",                "", "Q", "yoy",   "ECB", "policy_input",  "N"),
    ("ea_pmi_comp",  "유로존 합성 PMI",  "EA", "growth",    "manual","EA_PMI_COMPOSITE",                   "", "M", "level", "ECB", "policy_input",  "N"),
    ("de_ifo",       "독일 IFO 기업환경","EA", "sentiment", "manual","DE_IFO_BCI",                         "", "M", "level", "ECB", "policy_input",  "N"),
    ("ea_unrate",    "유로존 실업률",    "EA", "labor_slack","ecb", "LFSI.M.I9.S.UNEHRT.TOTAL0.15_74.T",   "", "M", "level", "ECB", "policy_input",  "N"),
    ("ea_dfr",       "ECB 예금금리(DFR)","EA", "policy",    "ecb",  "FM.D.U2.EUR.4F.KR.DFR.LEV",           "", "D", "level", "ECB", "outcome",       "N"),
    ("ea_mro",       "ECB 정책금리(MRO)","EA", "policy",    "ecb",  "FM.D.U2.EUR.4F.KR.MRR_FR.LEV",        "", "D", "level", "ECB", "outcome",       "N"),
    ("de_bund2y",    "독일 국채 2년",    "EA", "financial", "ecb",  "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y",  "", "D", "level", "ECB", "kr_link_input", "N"),
    ("de_bund10y",   "독일 국채 10년",   "EA", "financial", "ecb",  "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y", "", "D", "level", "ECB", "kr_link_input", "N"),

    # ================================================================== 일본
    ("jp_cpi_exfresh","전국 CPI(신선식품 제외)","JP", "inflation",   "estat", "JP_CPI_COREA",  "", "M", "yoy",   "BOJ", "policy_input",  "N"),
    ("jp_tokyo_cpi",  "도쿄 CPI(신선식품 제외)","JP", "inflation",   "estat", "JP_TOKYO_CORE", "", "M", "yoy",   "BOJ", "policy_input",  "N"),
    ("jp_tankan",     "단칸 대기업제조업 DI",   "JP", "sentiment",   "boj",   "TANKAN_LMFG_DI","", "Q", "level", "BOJ", "policy_input",  "N"),
    ("jp_real_wage",  "실질임금",               "JP", "labor_level", "mhlw",  "JP_REAL_WAGE",  "", "M", "yoy",   "BOJ", "policy_input",  "N"),
    ("jp_shunto",     "춘투 임금인상률",        "JP", "labor_level", "manual","JP_SHUNTO",     "", "A", "level", "BOJ", "policy_input",  "N"),
    ("jp_jgb10y",     "JGB 10년",               "JP", "financial",   "mof",   "JGB_10Y",       "", "D", "level", "BOJ", "kr_link_input", "N"),
    ("jp_policy_rate","무담보콜 O/N 유도목표",  "JP", "policy",      "boj",   "BOJ_POLICY",    "", "D", "level", "BOJ", "outcome",       "N"),

    # ================================================================ 글로벌
    ("bdi",    "발틱운임지수",   "GL", "commodity", "scrape", "BDI",    "", "D", "log_chg", "NONE", "kr_link_input", "N"),
    ("scfi",   "상하이운임지수", "GL", "commodity", "scrape", "SCFI",   "", "W", "log_chg", "NONE", "kr_link_input", "N"),
    ("oil_wti","WTI 유가",       "GL", "commodity", "fred",   "DCOILWTICO", "", "D", "log_chg", "FED", "policy_input", "Y"),
    ("copper", "구리 가격",      "GL", "commodity", "fred",   "PCOPPUSDM",  "", "M", "log_chg", "NONE", "kr_link_input", "Y"),
    ("gold",   "금 가격",        "GL", "commodity", "scrape", "XAUUSD",     "", "D", "log_chg", "NONE", "market_baseline", "N"),
    ("dxy",    "달러인덱스",     "GL", "financial", "fred",   "DTWEXBGS",   "", "D", "log_chg", "FED", "market_baseline", "Y"),
    ("usdkrw", "원달러 환율",    "GL", "financial", "fred",   "DEXKOUS",    "", "D", "log_chg", "BOK", "kr_link_input",   "Y"),
    ("usdjpy", "엔달러 환율",    "GL", "financial", "fred",   "DEXJPUS",    "", "D", "log_chg", "BOJ", "kr_link_input",   "Y"),
]

HEADER = ["id", "name_ko", "country", "category", "source", "source_id", "item_code",
          "freq", "transform", "target_cb", "use_as", "hawkish_sign", "weight",
          "sign_note", "review_flag", "id_verified"]


def build():
    rows, seen = [], set()
    for (iid, name, country, cat, src, sid, item_code, freq, tf, cb, use_as, verified) in INDICATORS:
        assert iid not in seen, f"중복 id: {iid}"
        assert iid[0].isalpha(), f"id는 문자로 시작해야 함: {iid}"
        seen.add(iid)

        sign, note = resolve_sign(iid, cat)
        weight = resolve_weight(iid, cat)

        # 매파 스코어 집계는 use_as=policy_input 인 것만. 나머지는 부호/가중치 0.
        if use_as != "policy_input":
            sign, weight = 0, 0.0
            note = f"[{use_as}] 매파 스코어 집계 제외"
            flag = ""          # 의도적 0이므로 검토 대상 아님
        else:
            flag = needs_review(iid, cat, sign, weight)

        rows.append([iid, name, country, cat, src, sid, item_code, freq, tf, cb, use_as,
                     sign, weight, note, flag, verified])

    with open("../config/indicators.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return rows


if __name__ == "__main__":
    rows = build()
    print(f"총 {len(rows)}개 지표 생성")
    from collections import Counter
    print("국가별:", dict(Counter(r[2] for r in rows)))
    print("용도별:", dict(Counter(r[10] for r in rows)))
    print("검토필요:", sum(1 for r in rows if r[14]))
    print("id확인필요:", sum(1 for r in rows if r[15] == "N"))
