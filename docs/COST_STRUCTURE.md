# Sparrow AI: cost structure and pricing tables (26 Nov 2026)

Tags: **[R]** repo/config or your decision, **[W]** third-party web figure (not Meta's/CASAFARI's own rate card, verify), **[A]** my assumption (no data yet).

## 1. Who pays what

| Item | Paid by | Paid to | Amount | Tag |
|---|---|---|---|---|
| Setup | n/a | n/a | EUR 0 | R |
| Meeting scheduled by the assistant | Agent | Sparrow | EUR 30 each, billed when scheduled | R |
| First message and follow-ups (marketing template) | Agent | Meta | about EUR 0.0585 each | W |
| Reminders and confirmations (utility template) | Agent | Meta | about EUR 0.0166 each | W |
| AI replies inside the 24 h window | Agent | Meta | free up to 1,000 per number per month, then charged (rate not verified) | W |
| AI model (Azure OpenAI) | Sparrow | Microsoft | EUR 0.5-1.5 per booked meeting, all chats included | A |
| Hosting, database, monitoring | Sparrow | Railway etc. | EUR 10-25 per month in total (check your bill) | A |
| Card fees on the monthly charge | Sparrow | Stripe | about 1.5% + EUR 0.25 per charge | A |
| Prospecting data (CASAFARI) | Sparrow, or the agent with their own key | CASAFARI | EUR 0.40 per page of 10 results = EUR 0.08-0.27 per usable lead; EUR 49 per month Agent plan | R / W |

## 2. Conversions, inbound leads (owner asked), per 100 contacted

| | Low | Base | High | Tag |
|---|---|---|---|---|
| Booking rate of contacted leads | 10% | 20% | 30% | A |
| Meetings booked | 10 | 20 | 30 | |
| Sparrow revenue (EUR 30 each) | EUR 300 | EUR 600 | EUR 900 | R |
| Sparrow AI cost (about EUR 1 per meeting) | EUR 10 | EUR 20 | EUR 30 | A |
| Sparrow margin before card fees and hosting | EUR 290 | EUR 580 | EUR 870 | |
| Agent's Meta fees (3 marketing messages per lead + 2 reminders per meeting) | EUR 18.0 | EUR 18.2 | EUR 18.5 | W |
| Agent's all-in cost per meeting (fee + Meta) | EUR 31.8 | EUR 30.9 | EUR 30.6 | |

## 3. Conversions, cold prospecting (we wrote first), per 100 contacted

| | Low | Base | High | Tag |
|---|---|---|---|---|
| Booking rate | 1% | 2% | 3% | A |
| Meetings booked | 1 | 2 | 3 | |
| Sparrow revenue | EUR 30 | EUR 60 | EUR 90 | R |
| CASAFARI data cost (mid EUR 13 / worst EUR 27 per 100 leads) | EUR 13 / 27 | EUR 13 / 27 | EUR 13 / 27 | R / W |
| Sparrow AI cost (about EUR 2 per meeting) | EUR 2 | EUR 4 | EUR 6 | A |
| Sparrow margin (data EUR 13 / data EUR 27) | EUR 15 / 1 | EUR 43 / 29 | EUR 71 / 57 | |
| Agent's Meta fees (1 marketing message per lead, no follow-ups) | EUR 5.9 | EUR 5.9 | EUR 5.9 | W |

## 4. One agent per month, inbound (card fee included)

| Leads per month | Meetings (low / base / high) | Agent pays Sparrow | Sparrow margin after AI and card fee | Agent's Meta fees |
|---|---|---|---|---|
| 10 | 1 / 2 / 3 | EUR 30 / 60 / 90 | EUR 28 / 57 / 85 | about EUR 1.8 |
| 20 | 2 / 4 / 6 | EUR 60 / 120 / 180 | EUR 57 / 114 / 171 | about EUR 3.6 |
| 40 | 4 / 8 / 12 | EUR 120 / 240 / 360 | EUR 114 / 228 / 342 | about EUR 7.3 |

## 5. Monthly revenue by number of agents (20 inbound leads each: low / base / high)

| Agents | Revenue low | Revenue base | Revenue high | Sparrow costs at base (AI + card + EUR 25 fixed) | Margin at base |
|---|---|---|---|---|---|
| 5 | EUR 300 | EUR 600 | EUR 900 | EUR 55 | EUR 545 |
| 10 | EUR 600 | EUR 1,200 | EUR 1,800 | EUR 86 | EUR 1,114 |
| 25 | EUR 1,500 | EUR 3,000 | EUR 4,500 | EUR 176 | EUR 2,824 |
| 50 | EUR 3,000 | EUR 6,000 | EUR 9,000 | EUR 328 | EUR 5,672 |
| 100 | EUR 6,000 | EUR 12,000 | EUR 18,000 | EUR 630 | EUR 11,370 |

## 6. Pricing options: what the agent pays per month

| Model | 2 meetings | 4 meetings | 8 meetings | Effective EUR per meeting (2 / 4 / 8) |
|---|---|---|---|---|
| A. EUR 30 per meeting (today) | EUR 60 | EUR 120 | EUR 240 | 30 / 30 / 30 |
| B. EUR 50 per meeting | EUR 100 | EUR 200 | EUR 400 | 50 / 50 / 50 |
| C. EUR 49 per month + EUR 20 per meeting | EUR 89 | EUR 129 | EUR 209 | 44.5 / 32.3 / 26.1 |
| D. EUR 99 per month + EUR 10 per meeting | EUR 119 | EUR 139 | EUR 179 | 59.5 / 34.8 / 22.4 |

## 7. Prospecting add-on: what to charge

| Option | Agent pays | Sparrow keeps | Note |
|---|---|---|---|
| Agent's own CASAFARI key | EUR 49 per month to CASAFARI | EUR 30 per meeting | safest for CASAFARI's terms (no data shared across agents) |
| Sparrow's key, absorbed | nothing extra | EUR 30 - data cost (EUR 8-27 per 100 leads) | margin near zero at 1% booking, conflicts with CASAFARI's one-key rule |
| Sparrow's key, passed through | data cost + 20% (EUR 10-32 per 100 leads) | EUR 30 per meeting + 20% | needs CASAFARI's written yes |

## 8. Suggested starting point

| Decision | Suggestion |
|---|---|
| Fee | EUR 30 per meeting scheduled, no setup, no monthly minimum (pilot) |
| Collection | card on file at onboarding, one charge per month for the total |
| Credit | owner cancels or does not show and the agent tells us within 48 h: credit by hand |
| After 30 days | test EUR 50 on owner-requested meetings |
| Prospecting | agent's own CASAFARI key; stays off until the lawyer answers |
