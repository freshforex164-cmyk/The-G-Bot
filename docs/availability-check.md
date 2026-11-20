# Availability diagnosis — 2026-11-20

Read-only live check, DEMO SSID, `--run availability` (subscribe every mapped open symbol, wait 10 s).

## Library 0.2.15 (pinned at the time) — 3/56 ticking
```
Display name | Symbol | In live list | Subscribed | Tick in last 10s
AED/CNY | unmatched | no | no | no
AUD/CAD | AUDCAD | yes | yes | yes
AUD/CHF | AUDCHF | yes | yes | no
CAD/CHF | CADCHF | yes | yes | no
CAD/JPY | CADJPY | yes | yes | yes
CHF/JPY | CHFJPY | yes | yes | no
EUR/NZD | unmatched | no | no | no
GBP/USD | GBPUSD | yes | yes | yes
KES/USD | unmatched | no | no | no
MAD/USD | unmatched | no | no | no
NGN/USD | unmatched | no | no | no
NZD/USD | unmatched | no | no | no
QAR/CNY | unmatched | no | no | no
SAR/CNY | unmatched | no | no | no
USD/BDT | unmatched | no | no | no
USD/CLP | unmatched | no | no | no
USD/INR | unmatched | no | no | no
USD/VND | unmatched | no | no | no
YER/USD | unmatched | no | no | no
EUR/USD | EURUSD | yes | yes | no
OMR/CNY | unmatched | no | no | no
AED/CNY OTC | AEDCNY_otc | yes | yes | no
AUD/CAD OTC | AUDCAD_otc | yes | yes | no
AUD/CHF OTC | AUDCHF_otc | yes | yes | no
CAD/CHF OTC | CADCHF_otc | yes | yes | no
CAD/JPY OTC | CADJPY_otc | yes | yes | no
CHF/JPY OTC | CHFJPY_otc | yes | yes | no
EUR/NZD OTC | EURNZD_otc | yes | yes | no
GBP/USD OTC | GBPUSD_otc | yes | yes | no
KES/USD OTC | KESUSD_otc | yes | yes | no
MAD/USD OTC | MADUSD_otc | yes | yes | no
NGN/USD OTC | NGNUSD_otc | yes | yes | no
NZD/USD OTC | NZDUSD_otc | yes | yes | no
QAR/CNY OTC | QARCNY_otc | yes | yes | no
SAR/CNY OTC | SARCNY_otc | yes | yes | no
USD/BDT OTC | unmatched | no | no | no
USD/CLP OTC | USDCLP_otc | yes | yes | no
USD/INR OTC | USDINR_otc | yes | yes | no
USD/VND OTC | USDVND_otc | yes | yes | no
YER/USD OTC | YERUSD_otc | yes | yes | no
EUR/USD OTC | EURUSD_otc | yes | yes | no
OMR/CNY OTC | OMRCNY_otc | yes | yes | no
Avalanche OTC | AVAX_otc | yes | yes | no
Dogecoin OTC | DOGE_otc | yes | yes | no
Solana OTC | SOL-USD_otc | yes | yes | no
Brent Oil OTC | UKBrent_otc | yes | yes | no
WTI Crude Oil OTC | USCrude_otc | yes | yes | no
Silver OTC | XAGUSD_otc | yes | yes | no
Gold OTC | XAUUSD_otc | yes | yes | no
American Express OTC | #AXP_otc | yes | yes | no
FACEBOOK INC OTC | unmatched | no | no | no
Johnson & Johnson OTC | #JNJ_otc | yes | yes | no
McDonald's OTC | #MCD_otc | yes | yes | no
AUS 200 OTC | AUS200_otc | yes | yes | no
100GBP | 100GBP | yes | yes | no
100GBP OTC | 100GBP_otc | yes | yes | no
```

## Library 0.2.18 + UTC timestamp fix — 40/56 ticking
```
Display name | Symbol | In live list | Subscribed | Tick in last 10s
AED/CNY | unmatched | no | no | no
AUD/CAD | AUDCAD | yes | yes | yes
AUD/CHF | AUDCHF | yes | yes | yes
CAD/CHF | CADCHF | yes | yes | yes
CAD/JPY | CADJPY | yes | yes | yes
CHF/JPY | CHFJPY | yes | yes | yes
EUR/NZD | unmatched | no | no | no
GBP/USD | GBPUSD | yes | yes | yes
KES/USD | unmatched | no | no | no
MAD/USD | unmatched | no | no | no
NGN/USD | unmatched | no | no | no
NZD/USD | unmatched | no | no | no
QAR/CNY | unmatched | no | no | no
SAR/CNY | unmatched | no | no | no
USD/BDT | unmatched | no | no | no
USD/CLP | unmatched | no | no | no
USD/INR | unmatched | no | no | no
USD/VND | unmatched | no | no | no
YER/USD | unmatched | no | no | no
EUR/USD | EURUSD | yes | yes | yes
OMR/CNY | unmatched | no | no | no
AED/CNY OTC | AEDCNY_otc | yes | yes | yes
AUD/CAD OTC | AUDCAD_otc | yes | yes | yes
AUD/CHF OTC | AUDCHF_otc | yes | yes | yes
CAD/CHF OTC | CADCHF_otc | yes | yes | yes
CAD/JPY OTC | CADJPY_otc | yes | yes | yes
CHF/JPY OTC | CHFJPY_otc | yes | yes | yes
EUR/NZD OTC | EURNZD_otc | yes | yes | yes
GBP/USD OTC | GBPUSD_otc | yes | yes | yes
KES/USD OTC | KESUSD_otc | yes | yes | yes
MAD/USD OTC | MADUSD_otc | yes | yes | yes
NGN/USD OTC | NGNUSD_otc | yes | yes | yes
NZD/USD OTC | NZDUSD_otc | yes | yes | yes
QAR/CNY OTC | QARCNY_otc | yes | yes | yes
SAR/CNY OTC | SARCNY_otc | yes | yes | yes
USD/BDT OTC | unmatched | no | no | no
USD/CLP OTC | USDCLP_otc | yes | yes | yes
USD/INR OTC | USDINR_otc | yes | yes | yes
USD/VND OTC | USDVND_otc | yes | yes | yes
YER/USD OTC | YERUSD_otc | yes | yes | yes
EUR/USD OTC | EURUSD_otc | yes | yes | yes
OMR/CNY OTC | OMRCNY_otc | yes | yes | yes
Avalanche OTC | AVAX_otc | yes | yes | yes
Dogecoin OTC | DOGE_otc | yes | yes | yes
Solana OTC | SOL-USD_otc | yes | yes | yes
Brent Oil OTC | UKBrent_otc | yes | yes | yes
WTI Crude Oil OTC | USCrude_otc | yes | yes | yes
Silver OTC | XAGUSD_otc | yes | yes | yes
Gold OTC | XAUUSD_otc | yes | yes | yes
American Express OTC | #AXP_otc | yes | yes | yes
FACEBOOK INC OTC | unmatched | no | no | no
Johnson & Johnson OTC | #JNJ_otc | yes | yes | yes
McDonald's OTC | #MCD_otc | yes | yes | yes
AUS 200 OTC | AUS200_otc | yes | yes | yes
100GBP | 100GBP | yes | yes | yes
100GBP OTC | 100GBP_otc | yes | yes | yes
Assets with a tick in the last 10s: 40/56
```
