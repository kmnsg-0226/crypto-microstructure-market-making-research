# Passive quote-filter approach exploration

## 결론

기존 V1 결과에서 멈추지 않고, 동결된 feature와 fill label 위에서 총 `248`개의 서로
다른 primary 접근을 비교했다. 단일 신호 방향/강도, OBI·micro 합의, signal joint,
queue, spread, UTC session, signal×queue, signal×session, consensus×queue,
queue×session을 포함한다. 또한 1차 shortlist에 대해 quote lifetime 4개, markout
horizon 4개, queue model 2개를 교차해 `3,168`개 split/side sensitivity cell을
검사했다.

필터링은 adverse selection을 일관되게 줄였다. 1차 shortlist 10개와 2차 shortlist
10개 모두 각 8개 날짜에서 always-quote baseline보다 mean markout이 개선됐다. 그러나
combined bid+ask의 mean maker markout과 gross pre-fee edge는 모든 primary 및
sensitivity cell에서 여전히 음수였다. 따라서 현재 결과는 "덜 나쁜 quote 구간"을
찾은 것이지 수익 가능한 market-making 전략을 찾은 것이 아니다. Continuous MM,
inventory/PnL simulation, Sharpe 계산은 진행하지 않았다.

## 해석상 경계

이 분석은 새로운 unseen validation이 아니다. V1 결과를 본 뒤 1차 탐색 spec을
작성했고, 1차 결과를 본 뒤 2차 조합 spec을 작성했다. June과 Jul/Aug 데이터도 과거
milestone에서 이미 열어봤다. 따라서 모든 later-date 비교는 retrospective replication
또는 descriptive diagnostic으로만 해석한다. 진짜 검증은 새 규칙을 고정한 이후 현재
수집 중인 native Binance data에서 해야 한다.

Audit artifacts:

- 1차 spec: `research/specs/passive_approach_exploration_spec.json`
- 1차 spec SHA-256: `50f7f1c4ec84aec11db5ac16d54657eb9216d09a60e481494888ecaf6506d660`
- 2차 spec: `research/specs/passive_approach_combinations_spec.json`
- 2차 spec SHA-256: `6e7e566ecbd3255da3af06a09eb2c4aac99b00f7518301f5d632219f2c8e1080`
- outcome-free threshold SHA-256: `15523628391fcdbc74d8a6263a3c76e408cc67c7f99f0ff71d1502ee719ff6f2`
- 1차 shortlist SHA-256: `28dd28780f5eeabb4456b219170da57191e9b55c85b03aff93a2d4dc30255c65`
- 2차 result SHA-256: `9ddffbbf0a301ec2546e86f93e2dc9e64c77a2be52336830514ea7c87583ddb6`

## 검사한 접근

1차에는 baseline을 포함해 77개를 고정했다.

- 8개 신호 각각에 trend/contrarian tail 20%, half, broad 80%, central 60% 적용
- OBI L1/L5/L10 다수결과 prediction/OFI/TI 다수결
- combined prediction과 OBI-L5 동시 동의
- queue-ahead 하위 20%, 중간 60%, 상위 20%
- one-tick/wide spread
- UTC 00–08, 08–16, 16–24

2차에는 171개 AND 조합을 추가했다.

- 8 signal × 4 directional rules × 3 queue bands: 96
- 8 signal × 2 tail directions × 3 UTC sessions: 48
- 3 composite signals × 2 directions × 3 queue bands: 18
- 3 queue bands × 3 UTC sessions: 9

Primary 비교는 pessimistic visible-queue, quote lifetime 1초, post-fill markout 1초로
고정했다. Threshold는 maker outcome을 읽기 전에 Jan–May feature distribution만으로
기록했다. Fill과 alpha feature는 재학습하거나 다시 만들지 않았다.

## 1차 결과

개발 순위 1위는 `obi_l1__contrarian_tail20`이었다. Bid는 OBI-L1이 개발 q20
`-0.658832` 이하일 때, ask는 q80 `0.667371` 이상일 때만 quote한다. 이는 V1의
trend-side rule과 반대 방향이다.

| 구간 | always quote ticks | filter ticks | 개선 ticks | candidate 유지 | labeled fills | gross pre-fee bps |
|---|---:|---:|---:|---:|---:|---:|
| Jan–May development | -64.01 | -56.07 | +7.94 | 20.00% | 714,470 | -0.764 |
| June retrospective | -61.64 | -55.46 | +6.18 | 19.01% | 128,268 | -0.768 |
| Jul/Aug retrospective | -51.79 | -46.66 | +5.13 | 16.19% | 159,579 | -0.780 |

1차 shortlist에는 OBI L1/L5/L10, weighted OBI, weighted-mid displacement, combined
prediction의 contrarian tail과 OBI/micro consensus, prediction+OBI joint, low queue가
포함됐다. 80개 policy-day 비교가 모두 baseline 대비 양수였고 개선 범위는
`+3.55`~`+9.36` ticks였다. 하지만 shortlist 최선도 절대 markout과 gross bps가
모두 음수였다.

같은 tail에서 방향만 바꾸면 차이가 크다. Development의 OBI-L1 trend tail은
`-83.88` ticks, contrarian tail은 `-56.07` ticks였다. Combined prediction도 trend
`-81.95`, contrarian `-56.63` ticks였다. 이는 frozen alpha가 taker 방향 예측에는
유용해도 같은 방향 maker quote의 fill selection에는 반대로 작동한다는 V1 진단과
일치한다.

## 2차 조합 결과

개발 5일의 worst-day 우선 순위 1위는
`weighted_mid_minus_mid_ticks__contrarian_tail20__utc_00_08`이었다. Bid는
weighted-mid displacement가 q20 `-0.329735` ticks 이하, ask는 q80 `0.334010`
ticks 이상이면서 UTC 00:00–08:00일 때만 quote한다.

| 구간 | always quote ticks | combination ticks | 개선 ticks | candidate 유지 | labeled fills | gross pre-fee bps |
|---|---:|---:|---:|---:|---:|---:|
| Jan–May development | -64.01 | -53.34 | +10.66 | 6.59% | 213,962 | -0.731 |
| June retrospective | -61.64 | -56.25 | +5.39 | 6.89% | 38,834 | -0.766 |
| Jul/Aug retrospective | -51.79 | -44.24 | +7.55 | 5.70% | 58,042 | -0.748 |

2차 shortlist의 80개 policy-day도 모두 개선됐고 범위는 `+4.48`~`+15.01` ticks였다.
UTC 00–08 결합 외에 `TI contrarian tail × queue bottom20`과
`OBI contrarian tail × queue bottom20`도 모든 날짜에서 개선됐다. 하지만 표에서 보듯
개선 후의 gross edge는 여전히 약 `-0.73`~`-0.77` bps다.

2차 전체 1,548 split/side cell 가운데 양수는 하나뿐이었다. June bid의
`weighted-mid trend tail × queue bottom20`이 `+21.23` ticks였지만 candidate retention
`0.029%`, labeled fill `92`개에 불과해 사전 minimum retention/fill 조건을 모두
통과하지 못했다. Combined bid+ask cell은 2차에서도 `0`개가 양수였다. 이 극소 표본을
edge로 해석하거나 선택하지 않는다.

## Lifetime, horizon, queue sensitivity

1차 shortlist와 baseline에 대해 100/500/1,000/5,000ms quote lifetime,
100/500/1,000/5,000ms markout horizon, pessimistic visible queue와 optimistic
front-of-queue upper bound를 모두 비교했다.

- combined-side sensitivity cell: `1,056`
- bid/ask 별도 포함 cell: `3,168`
- 양수 mean markout cell: `0`
- combined-side 최선: `-2.14` ticks
- combined-side 최악: `-80.45` ticks

Optimistic queue bound는 selection을 크게 줄였지만 양수로 바꾸지는 못했다. 따라서
현재 negative result가 pessimistic FIFO 근사 하나에만 의존한다고 볼 수 없다.

## 다음 규칙 후보와 다음 단계

새 native data에 forward-only로 고정해 볼 진단 후보는 세 가지다.

1. 단순성 우선: OBI-L1 contrarian tail 20%
2. regime 결합: weighted-mid contrarian tail 20% + UTC 00–08
3. queue 결합: TI contrarian tail 20% + queue-ahead bottom 20%

이 후보들은 아직 거래 규칙이 아니라 forward evaluation rule이다. 새 수집 데이터에서
threshold를 다시 맞추지 않고, fill/markout 정의를 바꾸지 않은 채 평가해야 한다.
그 결과가 fee 전후 모두 양수이고 날짜별 안정성까지 유지될 때만 inventory-aware
market-maker 구현 여부를 다시 판단한다.

## 재현

```bash
.venv/bin/python -m pyresearch.passive.approach_search thresholds
.venv/bin/python -m pyresearch.passive.approach_search development
.venv/bin/python -m pyresearch.passive.approach_search replication
.venv/bin/python -m pyresearch.passive.approach_search combinations
.venv/bin/python -m pyresearch.passive.approach_reporting
.venv/bin/python -m unittest tests.test_passive_approach_search -v
```

상세 CSV/JSON은 git에서 제외된
`data/research/tardis/reports/passive/approach_exploration/` 아래에 있다. Summary
builder는 row count, unique aggregate key, shortlist 8일 완전성, positive-cell count와
모든 주요 artifact hash를 검증한다.
