#!/usr/bin/env python3
"""Robustness grid around the winning time-only blowoff fade rule."""

from backtest_fade import (
    RuleSet,
    bootstrap_mean,
    load_or_fetch,
    pct,
    run_rule,
    summarize,
)


def main() -> None:
    ohlc = load_or_fetch(refresh=False)

    variants = [
        RuleSet(
            "G1_HOLD1",
            "Blowoff entry, time exit 1d",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=1,
            take_profit=None,
            stop_loss=None,
        ),
        RuleSet(
            "G2_HOLD2",
            "Blowoff entry, time exit 2d",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=2,
            take_profit=None,
            stop_loss=None,
        ),
        RuleSet(
            "G3_HOLD3",
            "Blowoff entry, time exit 3d",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=None,
            stop_loss=None,
        ),
        RuleSet(
            "G5_HOLD5",
            "Blowoff entry, time exit 5d",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=5,
            take_profit=None,
            stop_loss=None,
        ),
        RuleSet(
            "G3_LIQ25k",
            "Blowoff + liq 25k, time 3d",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=None,
            stop_loss=None,
            min_vol_usd=25_000,
        ),
        RuleSet(
            "G3_LIQ50k",
            "Blowoff + liq 50k, time 3d",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=None,
            stop_loss=None,
            min_vol_usd=50_000,
        ),
        RuleSet(
            "G3_WIDE_SL50",
            "Blowoff, hold3, SL50% only no TP",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=None,
            stop_loss=0.50,
        ),
        RuleSet(
            "G3_SOFT_TP30",
            "Blowoff, hold3, TP30% no SL",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=0.30,
            stop_loss=None,
        ),
        RuleSet(
            "G3_NO_RSI",
            "Pump40+vol3+sma20, time3, no RSI",
            pump_min=0.40,
            min_rsi=None,
            min_vol_spike=3,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=None,
            stop_loss=None,
        ),
        RuleSet(
            "G3_VOL5",
            "Pump40+RSI70+vol5+sma20, time3",
            pump_min=0.40,
            min_rsi=70,
            min_vol_spike=5,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=None,
            stop_loss=None,
        ),
    ]

    header = f"{'Rule':<16} {'n':>5} {'WR':>8} {'E[net]':>9} {'Med':>9} {'PF':>6} {'P(m>0)':>8} {'avgLoss':>9}"
    print(header)
    print("-" * len(header))

    details = {}
    for r in variants:
        tr = run_rule(ohlc, r)
        s = summarize(r, tr)
        boot = bootstrap_mean([t.net_pnl for t in tr]) if tr else {}
        pf = f"{s['profit_factor']:.2f}" if s.get("profit_factor") else "n/a"
        ppos = f"{boot.get('p_mean_gt_0', 0) * 100:.1f}%" if boot else "n/a"
        print(
            f"{r.name:<16} {s['n']:>5} {pct(s['win_rate']):>8} {pct(s['expectancy']):>9} "
            f"{pct(s['med_net']):>9} {pf:>6} {ppos:>8} {pct(s['avg_loss']):>9}"
        )
        details[r.name] = {"summary": s, "boot": boot}

    print("\n=== Year stability (selected) ===")
    for name in ("G3_HOLD3", "G3_LIQ25k", "G3_LIQ50k", "G3_VOL5", "G5_HOLD5"):
        s = details[name]["summary"]
        boot = details[name]["boot"]
        print(f"\n{name}: n={s['n']} E={pct(s['expectancy'])} med={pct(s['med_net'])}")
        print(f"  MFE={pct(s['mean_mfe'])} MAE={pct(s['mean_mae'])}")
        print(
            f"  boot CI [{pct(boot.get('ci05'))}, {pct(boot.get('ci95'))}] "
            f"P>0={boot.get('p_mean_gt_0', 0) * 100:.1f}%"
        )
        for y, v in s["by_year"].items():
            print(
                f"  {y}: n={v['n']:<4} mean={pct(v['mean_net']):>9} wr={pct(v['win_rate']):>8} sum={pct(v['sum_net'])}"
            )


if __name__ == "__main__":
    main()
