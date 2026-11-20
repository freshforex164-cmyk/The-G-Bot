# GBot status — audit checkpoint, 2026-11-19

## Goal
- Read-only Pocket Option Telegram signals; preserve current 60-second candles/expiry.
- Repair asset availability, fresh feed payouts and one menu per chat without changing Render.

## Implemented
- No bot repairs applied; source audited separately from the Lovable template.
- Audit findings and evidence recorded in GBOT_AUDIT.md.

## Verified live
- None in this run. Owner reports Render deployment passes; independent check NOT RUN.

## Mock-only / offline
- Current GitHub main confirmed by API: 8808f4526deea24ab2dc5009de0e81e212644513.
- Full baseline: 141 tests, 17 failures, 41 reported errors, 0 skipped.
- Price-action configuration/classification 7 pass; assets 5 pass; outcomes 4 pass.
- Setup suite: 10 run, 2 pass, 2 failures, 6 errors due to short-history fixtures.
- Warmed-up mocked-UPTREND regression: pullback SELL then red candle above low BUY; PASS.
- Loopback health/admin tests 3 pass; decision store 1 pass.
- Startup partial candle reproduced offline; no engine/timing edits applied.

## Known issues
- Step 1/7: obsolete test APIs and fixtures prevent full acceptance; fix tests without weakening production rules.
- Step 2: live availability cause NOT CONFIRMED; exact-name matching can fail; actual provider symbols needed.
- Step 2: Solana OTC exists in assets.json; live mapping and tick availability NOT RUN.
- Step 3: one five-second probe; permanent stream starts after history; add two ten-second attempts and default-30 cap.
- Step 3: 600-second unused grace exists; verify actual unsubscribe with tests.
- Step 4: payouts retained in asset metadata; use fresh exact-symbol read and explicit unavailable fallback.
- Step 4: account label, disclosure and payouts CLI still needed.
- Step 5: navigation sends new messages; no tracked menu ID/edit/delete implementation.
- Step 6: startup partial bucket is emitted; add diagnostic CLI and failing-rule coverage.
- Step 7: validity config 15 seconds versus message/queue 10; active ATR conflicts with prior brief, engine preserved.
- Step 9: all requested real provider/Telegram/Render checks NOT RUN; no production configuration here.
- Step 10: existing GitHub bot is not this Lovable working tree; commit/push NOT RUN.

## Needs your action
- Keep Render running; supply safe deployed diagnostics or approve a separate secure read-only test setup.
- Agree how tested source changes reach the existing bot repository; never paste secrets in chat.

## Next step
- Obtain actual live-symbol/subscription/tick evidence before an availability repair claim.
- Then scoped fixture/subscription/payout/menu repairs and final offline suite; no production poller overlap.

## Last run date
- 2026-11-19 UTC.