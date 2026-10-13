# Risk grades

This is the template for Defensive, Standard, Harvest, and Aggressive. A click on the Risk & Strategy page fills these appetite numbers. It does not rewrite a live account until that account's grade is clicked. Harvest is the new name for the Income card. A book saved as `income` still selects Harvest.

Paper is the acceptance preset. It is not one of these four.

## What a grade is

A grade is appetite. It decides how large a new open may be, how far the strike sits from the spot, how long the option may live, and how often the book may trade.

The reading stays one set of numbers for every grade. Those numbers define the measurement. Moving them would make the same market look different, rather than making the book take more or less risk.

A new short is ranked in two steps, in `rank_choices` and `_key`:

1. The expiry closest to the DTE target comes first. The chain menu only contains expiries from DTE min through DTE max.
2. Inside that expiry, the strike closest to the delta target comes first. The best annualized premium is the tie-break.

A lower delta target is the lower chance of assignment. The premium sort already pockets the better credit among strikes near that target. A grade does not need a new ranker. It sets the target, and the existing sort does the rest.

Min DTE new is a separate gate. A new open is blocked when the expiry is shorter than that floor. Today the four grades move this floor (21, 7, 14, 7) while DTE min stays 21 for every grade. A floor of 7 or 14 never reaches the menu, so the click looks like it only changes Min DTE new. In this template, Min DTE new equals DTE min.

Seven days is not used. A one-week option is hard to estimate and raises the chance of assignment. Fourteen days is only the aggressive floor.

## Page order

Left to right: Defensive, Standard, Harvest, Aggressive.

Defensive, Standard, and Harvest stay on a monthly menu, about 30 to 45 days. Harvest sells a closer strike than Standard and holds the credit longer. It does not use a shorter date. Aggressive is the only grade that shortens the menu and allows more orders.

## Shared measurement

| Knob | Value | Why it stays |
| --- | --- | --- |
| RV window | 20 | Sessions in the realized-vol reading |
| Min samples | 20 | Below this the rank stays off |
| RV high | 80 | Percentile that tags RV_HIGH |
| RV low | 20 | Percentile that tags RV_LOW |
| Wing factor | 0.50 | Scales distance. About a 0.28 delta. An assumption |
| RV band high | 45 | Realized vol at or above this uses the high multiple |
| RV band mid | 25 | Realized vol at or above this, and below the high band, uses the mid multiple |
| RV multiple high / mid / low | 1.05 / 1.35 / 1.80 | Assumptions that turn realized vol into a width |
| IVR high | 80 | A line on the phase chart. It does not tag a symbol |
| Screener top N | 3 | How many ranked strikes the table shows |
| Covered-call rail | 0.80 | Sell a covered call when price is this far up the 63-session channel |
| Max position / symbol | 0 | Off. The contract cap is the size tool |

## Appetite

DTE is min / target / max. CSP delta is low / target / high. A ceiling of 0 means the volatility width has no far cap.

### Defensive

Smallest book, most cash, farthest strike, longest monthly target. Among strikes near 0.16 delta, the existing sort keeps the better premium.

| Knob | Value |
| --- | --- |
| Max symbol % NAV | 10 |
| Cash reserve % | 40 |
| Daily loss halt % | 3 |
| Max contracts / symbol | 1 |
| Max orders / day | 4 |
| Seller margin budget % | 30 |
| Earnings blackout days | 10 |
| Min RV percentile new short | 60 |
| Min DTE new | 30 |
| DTE min / target / max | 30 / 45 / 60 |
| CSP delta low / target / high | 0.08 / 0.16 / 0.25 |
| Covered-call delta | 0.20 |
| Strangle delta | 0.16 |
| Profit take % | 40 |
| OTM floor % | 8 |
| OTM ceiling % | 0 |
| Chain band % | 35 |
| Max assignment % NAV | 25 |
| Min cash after assignment % | 25 |
| Assignment delta | 0.20 |
| Max bucket % NAV | 20 |

Profit-take 40 closes a short once 40% of the credit is captured, so the position does not stay open for the rest of the decay. OTM floor 8 keeps the width from collapsing toward the spot. Ceiling 0 lets a high realized vol push the wing farther out. Chain band 35 is wide enough that those far strikes are in the stored menu. Assignment delta 0.20 counts more short puts as likely assignments, so the assignment cap binds sooner.

### Standard

The book Athena starts with. Monthly menu, delta target 0.25, the current cash-secured-put band.

| Knob | Value |
| --- | --- |
| Max symbol % NAV | 20 |
| Cash reserve % | 30 |
| Daily loss halt % | 5 |
| Max contracts / symbol | 3 |
| Max orders / day | 8 |
| Seller margin budget % | 50 |
| Earnings blackout days | 7 |
| Min RV percentile new short | 50 |
| Min DTE new | 30 |
| DTE min / target / max | 30 / 35 / 45 |
| CSP delta low / target / high | 0.10 / 0.25 / 0.40 |
| Covered-call delta | 0.30 |
| Strangle delta | 0.28 |
| Profit take % | 50 |
| OTM floor % | 5 |
| OTM ceiling % | 40 |
| Chain band % | 25 |
| Max assignment % NAV | 40 |
| Min cash after assignment % | 15 |
| Assignment delta | 0.30 |
| Max bucket % NAV | 35 |

DTE target 35 sits in the middle of the 30 to 45 day window. The chain band and the assignment numbers match the defaults already used when a grade does not set them.

### Harvest

Same monthly window as Standard. The strike is closer, so each contract collects more premium. Size stays smaller than Aggressive, and the order cap stays with Standard, because this grade is not a shorter or busier book.

| Knob | Value |
| --- | --- |
| Max symbol % NAV | 25 |
| Cash reserve % | 25 |
| Daily loss halt % | 5 |
| Max contracts / symbol | 2 |
| Max orders / day | 8 |
| Seller margin budget % | 45 |
| Earnings blackout days | 7 |
| Min RV percentile new short | 45 |
| Min DTE new | 30 |
| DTE min / target / max | 30 / 35 / 45 |
| CSP delta low / target / high | 0.15 / 0.30 / 0.40 |
| Covered-call delta | 0.35 |
| Strangle delta | 0.30 |
| Profit take % | 65 |
| OTM floor % | 4 |
| OTM ceiling % | 20 |
| Chain band % | 25 |
| Max assignment % NAV | 35 |
| Min cash after assignment % | 15 |
| Assignment delta | 0.30 |
| Max bucket % NAV | 30 |

Two contracts, not three. A 0.30 delta uses more of the book per contract than Standard's 0.25. Profit-take 65 holds that closer credit until most of it is captured. OTM ceiling 20 stops the width from wandering out to the defensive wing. The chain band stays 25 because the target is closer than Defensive, so the extra discovery width is not needed.

### Aggressive

The only shorter menu, and the only grade that raises the daily order cap. The floor is 14 days, not 7.

| Knob | Value |
| --- | --- |
| Max symbol % NAV | 35 |
| Cash reserve % | 15 |
| Daily loss halt % | 8 |
| Max contracts / symbol | 4 |
| Max orders / day | 12 |
| Seller margin budget % | 60 |
| Earnings blackout days | 5 |
| Min RV percentile new short | 30 |
| Min DTE new | 14 |
| DTE min / target / max | 14 / 21 / 35 |
| CSP delta low / target / high | 0.20 / 0.35 / 0.45 |
| Covered-call delta | 0.40 |
| Strangle delta | 0.35 |
| Profit take % | 45 |
| OTM floor % | 3 |
| OTM ceiling % | 15 |
| Chain band % | 20 |
| Max assignment % NAV | 50 |
| Min cash after assignment % | 8 |
| Assignment delta | 0.40 |
| Max bucket % NAV | 45 |

DTE target 21 is inside a menu that still reaches 35 days, so a monthly expiry remains available when it is the closest listed one. The 0.35 delta target is the closer, higher-assignment choice. Profit-take 45 recycles a little sooner than Standard. OTM ceiling 15 and chain band 20 keep the stored menu near that closer strike. Assignment delta 0.40 counts only the closer short puts, so the larger assignment cap is not filled by far wings.

## Form

A grade click fills the ten limit boxes and the appetite knobs in this file. Min DTE new and DTE min are written together. The card label is Harvest. A book saved earlier as `income` still lights that card. The next save of those numbers stores `harvest`. Measurement knobs are not part of the click. Paper is unchanged.
