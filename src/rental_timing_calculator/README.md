# NYC Apartment Search Timing Strategy

**Owner:** Gage Anderson
**Current lease:** Embassy House, Apt 17D, 301 East 47th Street, NY 10017
**Current lease term:** 10/24/2025 → **4/30/2027**
**Current monthly rent:** $5,795
**Co-tenant:** Abhi Desai (joint & several liability)
**Target move window:** Winter / early spring 2027 (to capture seasonal discount)
**Last updated:** 2026-08-25

## Goal

Minimize total effective cost of the next lease while respecting the hard constraints of the current lease.

Primary trade-off:
- Winter leases (Dec–Feb) carry a lower sticker price (~1.7–2.0% below annual average, up to ~4.2% vs summer peak).
- Starting the new lease before 4/30/2027 creates an overlap period during which both rents must be paid.

**Allowed strategy under current lease:**
Double-pay for a limited number of weeks/months + gradual move of belongings.
This does **not** violate the lease. Breaking the lease early or unauthorized subletting does.

## Core Formula

Let:
- \( R \) = expected monthly rent of the target apartment in an “average” month
- \( \delta_m \) = seasonal price delta for lease-start month \( m \) (see table below)
- \( D \) = number of overlap days = \(\max(0,\; 2027\text{-}04\text{-}30 - \text{new start date})\)
- \( R_m = R \times (1 + \delta_m) \)

**Effective first-year cost** of starting in month \( m \):

\[
C_m = 12 \times R_m + D \times \frac{R_m}{30}
\]

Choose the start month \( m^* \) that minimizes \( C_m \), subject to:
- Realistic search lead time (30–45 days of active hunting)
- Inventory / competition constraints (thinner in deep winter)
- Personal cash-flow limit on maximum acceptable \( D \)

### Break-even overlap days (vs pure April/May start)

Against a no-overlap April 30 start (\(\delta \approx +0.3\%\)):

\[
D_{\text{BE}} \approx 360 \times \frac{\delta_{\text{Apr}} - \delta_m}{1 + \delta_m}
\]

For a typical winter start (\(\delta_m \approx -1.8\%\)):

\[
D_{\text{BE}} \approx 7.7 \text{ days}
\]

Even 30–60 days of overlap is usually still net-positive on a full-year basis for a ~$4k unit once the seasonal discount compounds. Two full months of double-pay (~$8–12k extra) is the practical upper bound most people will tolerate.

## Seasonal Price Deltas (NYC, same-apartment fixed-effect)

Source: RentReboot 2026 seasonality report (2022–2024 data, controlling for overall rent growth). Peak-to-trough spread ≈ 4.2%.

| Month | δ (vs year avg) | New listings index | Notes |
|-------|-----------------|--------------------|-------|
| Dec   | –2.0%          | 0.69 (lowest)     | Cheapest sticker + strong negotiation |
| Jan   | –1.9%          | 0.84              | Very good |
| Feb   | –1.7%          | 0.76              | Still strong |
| Mar   | –0.9%          | 0.92              | Transition |
| Apr   | +0.3%          | 0.96              | Near average |
| May   | +1.1%          | 1.21              | Rising |
| Jun   | +1.6%          | 1.28              | High |
| Jul   | +2.2%          | 1.33 (highest)    | Peak price + peak inventory |
| Aug   | +1.9%          | 1.24              | Still expensive |
| Sep   | +0.8%          | 1.03              | Falling |
| Oct   | –0.1%          | 0.92              | Negotiation power rising |
| Nov   | –1.4%          | 0.80              | Good value + high % below ask |

Negotiation power (share of leases signed below asking) peaks in fall (Oct–Nov ~29–31%) and remains solid in winter.

## Hard Constraints from Current Lease

### Allowed
- Signing a new lease that starts before 4/30/2027.
- Paying both rents during any overlap period.
- Gradually moving belongings over weeks or months.
- Keeping the current unit as the legal leased premises until the exact end date (keys returned, fully cleaned, all rent paid).

### Not allowed / high-risk
- Permanently vacating and surrendering the unit before 4/30/2027 without written landlord consent → default (Art. 38(1)(4)). Landlord can claim remaining rent + re-letting costs (Art. 39).
- Unauthorized sublet or assignment.
- Sublet that cannot satisfy the “primary residence + intent to re-occupy” test (Art. 22). A permanent move makes a clean sublet difficult.
- Any early termination agreement that is not in writing and signed by both parties (Art. 48 / Rider 64). Simply returning keys does **not** end the lease.

### Other operational constraints
- Landlord may begin showing the apartment to prospective tenants **three months prior** to lease end (i.e., from ~1 Feb 2027) with reasonable notice (Art. 37(C)).
- Joint & several liability with Abhi Desai. Any plan requires his coordination.
- Security deposit returned only after full, clean vacation on or before 4/30/2027 and payment of all amounts due.
- Renter’s insurance remains required on the current unit for the entire term.

## Recommended Decision Framework

1. **Set maximum tolerable overlap** (e.g., 45 or 60 days) based on cash reserves and lifestyle tolerance for a split household.
2. **Run the formula** for candidate start months (Feb 1, Mar 1, Apr 1, May 1) using your real target \( R \).
3. **Prefer** a February or early-March start if the net savings remain attractive after overlap cost and after applying a 2–3% “haircut” to the published seasonal deltas (market conditions can compress the discount).
4. **Default safe path** if cash-flow or roommate coordination is tight: search for a late-April / May 1 start with ≤15 days overlap. Forgoes most of the seasonal discount but eliminates almost all double-pay risk and lease friction.
5. **Never** rely on an early-termination or sublet rescue unless you have written landlord approval in hand first.

## Search Execution Notes

- Active search window: begin intensive hunting 30–45 days before desired start date (alerts earlier).
- Winter inventory is thinner (~25–30% fewer new listings). Maintain a short list of backup neighborhoods.
- Keep application package ready (pay stubs, employment letter, credit, bank statements, ID). Strong Meta IC5 profile is a material advantage.
- Target all-in housing cost ≤ 25% of gross income (hard ceiling 30%).

## Key Risks That Can Derail the Math

- Actual 2026–27 seasonal discount turns out smaller than historical averages.
- Suitable units in priority neighborhoods (near Farley / Penn) simply do not appear in the chosen winter window.
- Roommate disagreement or change in personal circumstances.
- Cash-flow shock that makes the planned overlap painful.
- Landlord showings or other access issues during the final three months become more disruptive than expected.

## References

- Current lease: Embassy House 17D (signed 10/8/2025, term ends 4/30/2027).
- Seasonal data: RentReboot “NYC Rent Seasonality Data Report 2026”.
- Prior strategy discussion (market context, broker fees, neighborhood focus, 25% income guideline).

---

*This document is a planning artifact only. It does not constitute legal advice. Confirm any sublet or early-exit discussion in writing with Stellar Management before relying on it.*
