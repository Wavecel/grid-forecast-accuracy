# Business Case

## The scenario

You are a data analyst supporting the **load forecasting team** at a US balancing authority, or at a retail energy supplier that buys power inside one.

Every day at roughly 10:00, the operator publishes a **day-ahead forecast**: hour by hour, how much electricity the region will need tomorrow. Generation is scheduled and power is procured against that number.

The next day, reality arrives.

| Outcome | Position | Consequence |
|---|---|---|
| Actual **above** forecast | **Short** | Buy the shortfall in real time, usually at a premium |
| Actual **below** forecast | **Long** | Sell surplus back, often below what you paid |

Both cost money. At PJM's ~150 GW scale, **1% error ≈ 1,000 MW**.

## The gap this fills

Forecasting teams do track error — but typically as a single monthly MAPE figure. That number conflates two fundamentally different things:

- **Noise.** Weather is chaotic, human behaviour is variable. Largely irreducible.
- **Bias.** The model is systematically high or low under identifiable conditions. **Recalibratable.**

A team looking at "3.1% MAPE in July" cannot tell which they have, so they cannot tell whether there is anything to fix. Reporting the average is the analytical equivalent of a shrug.

**This project separates the two and locates the conditions under which bias appears.**

## Stakeholders and decisions

| Stakeholder | Question | Decision this changes |
|---|---|---|
| Load forecasting manager | Is our model degrading? | Open a recalibration ticket, and where to point it |
| Forecasting modeller | Under what conditions do we miss? | Which term of the model to revisit |
| Energy procurement | When are we systematically short? | Adjust day-ahead purchase margin |
| Operations planner | What should we prepare for? | Reserve posture for the next 72h |
| Trading / risk | Is error direction predictable? | Position sizing |

## The six questions the dashboard must answer

| | Question | Where it's answered |
|---|---|---|
| **What** | How accurate was the day-ahead forecast? | Page 1 KPIs, Page 2 trend |
| **Why** | What conditions drive the misses? | Page 3 — bias vs cooling degree hours |
| **Where** | Which BAs, which hours? | Page 2 scorecard, Page 3 hour profile |
| **When** | Which days, seasons, day types? | Page 2 trend, Page 3 day-type |
| **Better or worse?** | Versus baseline, versus last year? | `MAPE vs Baseline %`, `MAPE YoY %` |
| **So what?** | What should we do? | Page 1 narrative + alert queue |

## Success criteria

The project succeeds if a forecasting manager can, in under two minutes:

1. See whether yesterday was normal, and by how much it deviated from a like-for-like baseline.
2. Identify the specific hours that need review, ranked.
3. State whether error is noise or bias — and if bias, under what conditions.
4. Know what conditions the next 72 hours will bring.
5. Trust the numbers, because the data-quality checks are visible.

## What makes the analysis non-trivial

Anyone can chart actual against forecast. The analytical work is:

1. **Separating bias from noise** — the distinction that makes a finding actionable.
2. **Like-for-like baselining** — comparing a Monday to a Sunday would attribute a normal weekday load rise to a forecast failure. Baselines are trailing 28 days *of the same day type*.
3. **Relative anomaly thresholds** — 3% error is an alert for a normally-accurate BA and routine for a volatile one. Z-scores against each BA's own distribution, not a global cutoff.
4. **Peak-hour isolation** — capacity charges and scarcity pricing settle at the peak. Being right on average is no comfort if you were wrong when the system was stressed.
5. **Weather attribution** — bucketing by cooling degree hours and reporting *signed* bias per bucket is what turns "sometimes wrong" into "systematically low when hot."

## The finding

| Conditions | MAPE | Bias |
|---|---|---|
| Mild (< 5 CDH) | ~2.1% | **~0.0%** |
| Extreme heat (> 15 CDH) | ~3.0% | **+2.9%** |

The forecast doesn't merely become *noisier* in heat — it becomes **directionally wrong**.

That pattern is the signature of an under-modelled air-conditioning response: AC load is convex in temperature (each additional degree adds more load than the last, until saturation), and a model with a linear or under-curved temperature term will under-predict exactly at the top of the range.

**Recommendation:** review the cooling-response term for the affected BAs, prioritising ERCOT, and validate against the measured `MW per CDH` sensitivity the dashboard reports.

**Stated limitation:** this is correlational at hourly grain. It identifies a pattern worth investigating, not a proven cause.
