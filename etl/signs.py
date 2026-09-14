"""
hawkish_sign 규칙 엔진.

원칙
----
1. hawkish_sign 은 "지표 -> 통화정책" 방향만 담당한다.
   "정책 -> 자산" 방향은 position_matrix 에서 따로 관리한다. 섞으면 반드시 꼬인다.

2. 부호는 카테고리에서 결정론적으로 파생된다.
   생각이 바뀌면 EXCEPTIONS 나 SIGN_BY_CATEGORY 한 줄만 고치면 전체가 반영된다.

3. labor 를 level / slack 으로 쪼갠 것이 이 규칙의 핵심이다.
   NFP(+1) 와 실업률(-1) 이 한 카테고리에 섞이면 규칙이 성립하지 않는다.
"""

# ---------------------------------------------------------------- 카테고리 규칙

SIGN_BY_CATEGORY = {
    "inflation":    +1,   # 물가 상회 -> 긴축
    "inflation_exp": +1,  # 기대인플레 상회 -> 긴축 (연준이 직접 언급하는 변수)
    "growth":       +1,   # 성장 상회 -> 긴축
    "labor_level":  +1,   # 고용 수준 상회 -> 긴축 (NFP, JOLTS, 임금)
    "labor_slack":  -1,   # 유휴노동 상회 -> 완화 (실업률, 실업수당청구)
    "sentiment":    +1,   # 심리 상회 -> 긴축
    "external":     +1,   # 수출 등 대외 수요 상회 -> 긴축
    "credit":       +1,   # 가계부채/주택가격 상회 -> 긴축. 한은에서 특히 중요
    "financial":     0,   # 금리/스프레드 자체. 정책의 결과물이므로 입력에서 제외
    "policy":        0,   # 정책금리 자체. 결과 변수
    "commodity":    +1,   # 원자재 상회 -> 헤드라인 물가 자극
}

# ------------------------------------------------------------------- 예외 처리
# (sign, note) — 카테고리 규칙을 덮어쓴다.

EXCEPTIONS = {
    "us_umich_inflexp": (+1, "기대인플레 고착은 연준이 가장 민감하게 보는 변수. weight 상향"),
    "us_gdp_adv":       (+1, "성장 상회는 긴축 재료이나 분기 후행 발표라 시장 영향은 제한적"),
    "oil_wti":          (+1, "헤드라인만 자극. 연준 타깃은 근원이라 영향 약함 -> weight 0.3"),
    "copper":           (+1, "글로벌 수요 대리지표. 미 정책보다 한국 수출 링크에 유용"),
    "gold":             (-1, "실질금리와 역상관. 금 강세는 완화 기대 반영인 경우가 많음"),
    "bdi":              (+1, "실물 수요 대리지표이나 후행성/변동성 큼 -> weight 0.2"),
    "usdkrw":           ( 0, "미 정책의 종속변수. 한국 금리 블록 설명변수로만 사용"),
    "usdjpy":           ( 0, "BOJ 정책의 종속변수이자 개입 트리거. 입력 아님"),
    "dxy":              ( 0, "미 정책의 종속변수. 결과 변수로만 사용"),
}

# ------------------------------------------------------- 카테고리별 기본 가중치

DEFAULT_WEIGHT = {
    "inflation":    1.0,
    "inflation_exp": 1.0,
    "growth":       0.7,
    "labor_level":  0.8,
    "labor_slack":  0.8,
    "sentiment":    0.5,
    "external":     0.5,
    "credit":       0.6,
    "financial":    0.0,
    "policy":       0.0,
    "commodity":    0.3,
}

# 개별 상향/하향. 1.0 초과는 연준이 명시적으로 타깃하는 변수에만 허용한다.
WEIGHT_OVERRIDE = {
    "us_core_pce":       1.5,   # 연준 공식 타깃
    "us_core_cpi":       1.3,   # PCE보다 2~3주 먼저 나와 시장이 먼저 반응
    "us_nfp":            1.3,
    "us_cpi":            1.0,
    "us_umich_inflexp":  1.1,
    "us_unrate":         1.0,
    "us_iclaims":        0.6,   # 주간 노이즈 큼. 4주 이동평균으로 변환해 사용
    "oil_wti":           0.3,
    "bdi":               0.2,
    "copper":            0.2,
}


def resolve_sign(indicator_id: str, category: str):
    """(sign, note) 반환. 예외가 있으면 예외 우선."""
    if indicator_id in EXCEPTIONS:
        return EXCEPTIONS[indicator_id]
    if category not in SIGN_BY_CATEGORY:
        raise KeyError(f"미정의 카테고리: {category} ({indicator_id})")
    sign = SIGN_BY_CATEGORY[category]
    note = {
        +1: "값 상회 = 긴축 방향",
        -1: "값 상회 = 완화 방향",
         0: "정책의 결과 변수. 매파 스코어 집계에서 제외",
    }[sign]
    return sign, f"[{category}] {note}"


def resolve_weight(indicator_id: str, category: str) -> float:
    if indicator_id in WEIGHT_OVERRIDE:
        return WEIGHT_OVERRIDE[indicator_id]
    return DEFAULT_WEIGHT.get(category, 0.5)


def needs_review(indicator_id: str, category: str, sign: int, weight: float) -> str:
    """검토가 필요한 3가지 지점만 표시한다. 전부 볼 필요 없다."""
    flags = []
    if category == "labor_slack":
        flags.append("SLACK")        # 부호가 뒤집히는 자리
    if sign == 0:
        flags.append("ZERO")         # 정말 결과 변수가 맞는지
    if weight > 1.0:
        flags.append("HIGHW")        # 신호가 뭉개지지 않는지
    return "|".join(flags)


def hawkish_score(rows):
    """
    rows: [{'surprise_z': float, 'hawkish_sign': int, 'weight': float}, ...]
    반환: 가중 매파 스코어. 양수면 매파 우위.
    """
    num = sum(r["surprise_z"] * r["hawkish_sign"] * r["weight"] for r in rows)
    den = sum(abs(r["weight"]) for r in rows if r["hawkish_sign"] != 0)
    return num / den if den else 0.0
