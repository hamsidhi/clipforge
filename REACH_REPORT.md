# Reach report — YouTube Shorts (2026-11-08T19:05 UTC)

Channel: 1 subscribers, 294 lifetime views, 26 public videos. Analytics data through **2026-11-05** (YouTube Analytics lags 2–3 days; lifetime views below come from videos.list and are fresher).

## Top findings

1. **Nothing is invisible.** 0 of 43 uploads are locked private, rejected, region-blocked or 'made for kids'. The 17 private videos are scheduled (publishAt in the future) and the ones whose time passed did go public on time (e.g. qAlNc2xwQLo, su4LCQxwgt0, zK3N7c7rh3I on 8 Nov). The API project is NOT locking uploads.
2. **YouTube is barely testing the Shorts in the Shorts feed.** Only 2 of 26 public Shorts got any Shorts-feed views (50 feed views in total, 49 of them on ONE Short, i45TAoUBuzo). The biggest source is YouTube search (47% of views), then the channel page. Median 6.5 views per Short after 48 h (p25 4.0, p75 11.8).
3. **The one Short the feed did test lost most viewers early.** i45TAoUBuzo (57 s, Huberman): 49 feed views, 32.6% average view %, ~18 s watched on average, so the test stopped there. Over all Shorts with analytics, engaged views ÷ views = 0.53: about half the views end in the first seconds.
4. **Likely reasons for so little feed testing (not provable from the API, so listed as checks):** (a) the channel is 6 days old with 1 subscriber and posts 5+/day; (b) every clip comes from 4 very popular Figuring Out episodes (Sam Altman, Bryson DeChambeau, Huberman, Macron) that many clip channels also post. YouTube says reused content needs 'significant original commentary' or edits (YouTube Help → Channel monetization policies → Reused content: https://support.google.com/youtube/answer/1311392). That page is about monetization, but duplicates of widely posted moments also compete with the originals for the same viewers; (c) English 'Topic: Subtitle' titles on Hinglish clips.
5. **Patterns in our own data are too thin to tune slots or length.** Every group has n < 10 and most n < 5 (see below). Posting hour: no group has ≥ 5 Shorts. Length: ≤25 s Shorts have the best average view % (median 77.7, n=7) vs 26–35 s (29.1, n=5), but this is a hint, not proof. No slot is paused and length is unchanged; the weekly report will show if these hold.

## Funnel

| step | value |
|---|---|
| uploaded (uploads playlist) | 43 |
| public | 26 |
| scheduled (private until publishAt — normal) | 17 |
| invisible (private past publish time / rejected / blocked / made for kids) | 0 |
| public Shorts with ANY Shorts-feed views | 2 |
| Shorts-feed views (all Shorts) | 50 |
| lifetime views (public Shorts) | 304 |
| views per Short ≥48 h old: median / p25 / p75 (n) | 6.5 / 4.0 / 11.8 (18) |
| average view % (median over Shorts with analytics) | 58.1 |
| engaged views ÷ views | 0.53 |
| likes / comments | 18 / 0 |

Traffic sources (share of views, 28 days): YT_SEARCH 47%, SHORTS 32%, YT_CHANNEL 13%, SUBSCRIBER 4%, EXT_URL 3%, YT_OTHER_PAGE 1%, NO_LINK_OTHER 0%

## Buckets

Rules (ClipForge working thresholds, `reach:` in config.yaml): too new < 48 h public; no test traffic = < 50 views and < 20 Shorts-feed views; tested-rejected = average view % < 70.0.

| bucket | Shorts |
|---|---|
| invisible (private/locked/rejected/restricted) | 0 |
| scheduled (private until its publish time — normal) | 17 |
| too new (< 48 h public) | 8 |
| no test traffic (< 50 views after 48 h) | 16 |
| tested but rejected (feed views, weak retention) | 1 |
| working | 1 |

## Visibility checks

- made for kids: 0
- region blocked in india: 0
- rejected or failed: 0
- private past publish time: 0
- copyright claims / restrictions: not exposed by the Data API for your own videos — check YouTube Studio → Content → **Restrictions** column.

## Patterns in this channel's own data

Groups with n < 5 are shown but marked — too few to conclude anything.

**clip length → average view %**

| group | n | median |
|---|---|---|
| ≤25 s | 7 | 77.7 |
| 26–35 s | 5 | 29.1 |
| >45 s | 2 ⚠ | 60.4 |

**clip length → lifetime views**

| group | n | median |
|---|---|---|
| ≤25 s | 10 | 6.5 |
| 26–35 s | 5 | 5.0 |
| >45 s | 2 ⚠ | 56.0 |
| 36–45 s | 1 ⚠ | 4.0 |

**topic → views**

| group | n | median |
|---|---|---|
| success | 3 ⚠ | 3.0 |
| AI | 3 ⚠ | 11.0 |
| kidney donation | 1 ⚠ | 10.0 |
| media | 1 ⚠ | 4.0 |
| innovation | 1 ⚠ | 8.0 |
| speed | 1 ⚠ | 23.0 |
| injury | 1 ⚠ | 37.0 |
| Parenting | 1 ⚠ | 57.0 |
| Emotions | 1 ⚠ | 4.0 |
| startup | 1 ⚠ | 1.0 |
| startup funding | 1 ⚠ | 2.0 |
| future | 1 ⚠ | 11.0 |

**source episode → views**

| group | n | median |
|---|---|---|
| Sam Altman On AI Revolution, Superintell | 9 | 5.0 |
| How To Become Elite At Anything: Bryson  | 8 | 9.0 |
| Andrew Huberman: Become Mentally Dangero | 1 ⚠ | 75.0 |

**posting hour (IST) → views**

| group | n | median |
|---|---|---|
| 12 | 2 ⚠ | 12.5 |
| 1 | 2 ⚠ | 2.5 |
| 11 | 2 ⚠ | 40.0 |
| 20 | 1 ⚠ | 10.0 |
| 18 | 1 ⚠ | 4.0 |
| 13 | 1 ⚠ | 8.0 |
| 9 | 1 ⚠ | 3.0 |
| 7 | 1 ⚠ | 37.0 |
| 2 | 1 ⚠ | 57.0 |
| 3 | 1 ⚠ | 5.0 |
| 0 | 1 ⚠ | 2.0 |
| 23 | 1 ⚠ | 11.0 |

**guest name in title → views**

| group | n | median |
|---|---|---|
| False | 17 | 5.0 |
| True | 1 ⚠ | 37.0 |

**number in title → views**

| group | n | median |
|---|---|---|
| False | 15 | 8.0 |
| True | 3 ⚠ | 5.0 |

## Changes applied (future clips only)

- `reach.hook_rule: true` — the selection prompt gets a stricter first-sentence rule (start ON a concrete claim, name or number; skip moments that need a setup). Reason: ~half of views end in the first seconds (engaged ratio 0.53); the only feed-tested Short lost viewers at ~18 s.
- `reach.title_rules: true` — the title prompt asks for the guest's name and search words, no 'Topic: Subtitle'. Reason: search brings 47% of views; the feed brings almost none.
- Both rules apply to NEW selections only: they live in `reach:` (not in prompts/*.md or `select:`), so the selection fingerprint of existing jobs is unchanged and nothing already queued or uploaded changes.
- `reach.weekly_report: true` — every 7 days the scheduler writes workspace/reports/REACH_REPORT.md and sends a Telegram summary; Dashboard data at GET /api/publishing/reach.
- Not changed (not enough evidence yet): posting slots and count, clip length (clips.min_sec/max_sec), rendering.

## Per Short

| id | public (UTC) | len | views | feed | avg % | bucket | title |
|---|---|---|---|---|---|---|---|
| i45TAoUBuzo | 2026-11-04T05:44 | 57 | 75 | 49 | 33 | tested_rejected | Biggest Threat: SMI |
| OUaCCHnbgEk | 2026-11-04T05:44 | 21 | 5 | 0 | 109 | no_test_traffic | High Bar for AI Products #shorts |
| HH9QbPH8x18 | 2026-11-04T06:30 | 31 | 2 | 0 | 83 | no_test_traffic | Short-term pain for long-term gain #shorts |
| VfLfrUtjVOM | 2026-11-04T09:45 | 21 | 12 | 0 | 161 | no_test_traffic | AI optimism gap: US vs India #shorts |
| GOCdU2fZ8Ac | 2026-11-04T12:00 | 27 | 5 | 0 | 105 | no_test_traffic | Life's Crazy: Navigating Uncertainty #shorts |
| 5M7GeFDAw-c | 2026-11-04T13:45 | 21 | 11 | 0 | 78 | no_test_traffic | India's AI appetite #shorts |
| H2DHBbTAC_A | 2026-11-04T18:00 | 27 | 11 | 0 | 29 | no_test_traffic | AI's Future: Better Society? #shorts |
| yUghZT-M5s0 | 2026-11-04T18:30 | 21 | 2 | 0 | 0 | no_test_traffic | Best Time To Build #shorts |
| vYfOsRJvi2Y | 2026-11-04T20:15 | 20 | 1 | 0 | 0 | no_test_traffic | Biltabad Chee.d #shorts |
| 260lLHyb7XE | 2026-11-04T21:30 | 27 | 5 | 0 | 10 | no_test_traffic | 5 Friends, 5 Future #shorts |
| JMrmkCGlXro | 2026-11-05T20:05 | 21 | 4 | 0 | 38 | no_test_traffic | Feel Your Emotions |
| dYiEtLtgZzI | 2026-11-05T20:30 | 22 | 57 | 1 | 93 | working | Father's Anger: Lessons Learned |
| NUvTbY3wAWg | 2026-11-06T02:00 | 52 | 37 | 0 | 88 | no_test_traffic | Bryson’s Hand Injury: Golf’s Big Lesson |
| gsj_6PvaQJ4 | 2026-11-06T04:00 | 20 | 3 | 0 |  | no_test_traffic | Right Now? 2019 Decision |
| YvrK9PEEl5k | 2026-11-06T06:30 | 25 | 23 | 0 | 22 | no_test_traffic | 200 mph Swing Speed! |
| XlcBfS3tHxU | 2026-11-06T07:45 | 25 | 8 | 0 |  | no_test_traffic | Golf's YouTube Innovation: Golf's YouTube Bet |
| _T8MsD5R-aY | 2026-11-06T12:30 | 37 | 4 | 0 |  | no_test_traffic | Media Control: Golfers vs YouTube |
| Jy46wwUq93M | 2026-11-06T14:30 | 21 | 10 | 0 |  | no_test_traffic | Dad's Gift: Kidney Match |
| w_ZbtsSJh7g | 2026-11-07T13:00 | 40 | 3 | 0 |  | too_new | Macron's Security Fears for Tech |
| VlngwIGaAY0 | 2026-11-07T15:30 | 21 | 16 | 0 |  | too_new | Single-Length Clubs: Golf Innovation |
| gbDgVFa5D0Q | 2026-11-07T18:00 | 41 | 3 | 0 |  | too_new | Passion or Ego: Handling Criticism |
| A54PJQglv7I | 2026-11-08T07:00 | 23 | 7 | 0 |  | too_new | 24 Pounds in 30 Days | Diet Tips |
| lzU5P_4H750 | 2026-11-08T09:30 | 23 | 0 | 0 |  | too_new | Tech Leader's Journey: Sam Altman |
| qAlNc2xwQLo | 2026-11-08T13:00 | 22 | 0 | 0 |  | too_new | Always Better: Perspective for Success |
| su4LCQxwgt0 | 2026-11-08T15:00 | 28 | 0 | 0 |  | too_new | Scaling tech for big gains |
| zK3N7c7rh3I | 2026-11-08T16:30 | 57 | 0 | 0 |  | too_new | Mindfulness Matters: 15 Sec Break |
| mZmZntZiqDc | 2026-11-09T07:00 | 22 | 0 | 0 |  | scheduled | Altman On Unlikely Success |
| sPEOpHCrNOA | 2026-11-09T09:30 | 28 | 0 | 0 |  | scheduled | Childhood Insecurities & Rejection |
| wwcF9qLgiK8 | 2026-11-09T13:00 | 23 | 0 | 0 |  | scheduled | Nerd & Cool: Sam Altman's Childhood |
| z1m7R560c6Y | 2026-11-09T15:00 | 20 | 0 | 0 |  | scheduled | Perfection Obsession: Why I Always Ask Why |
| Wb71jIHgRhU | 2026-11-09T16:30 | 24 | 0 | 0 |  | scheduled | Compounding Curve: Career Growth |
| i98N-X-HPvc | 2026-11-10T07:00 | 21 | 0 | 0 |  | scheduled | Gaming Shaped Bryson’s Mindset |
| 7APSBv8s9g4 | 2026-11-10T09:30 | 35 | 0 | 0 |  | scheduled | AI in Science: Discovering New Math |
| i2-1t8fY9AI | 2026-11-10T13:00 | 26 | 0 | 0 |  | scheduled | Tiger's Intensity: How Compete? |
| HmYy-WvKQEk | 2026-11-10T15:00 | 22 | 0 | 0 |  | scheduled | Innovation: Why Herd Mentality |
| 4mrlKKb2OrM | 2026-11-10T16:30 | 23 | 0 | 0 |  | scheduled | Speed Training Over Strength |
| UacC-PLodqU | 2026-11-11T07:00 | 21 | 0 | 0 |  | scheduled | Future Hope: Optimism In AI |
| nq_ag1fhAeA | 2026-11-11T09:30 | 28 | 0 | 0 |  | scheduled | Top pe fall kar sakte hain |
| ireNnk4p2XI | 2026-11-11T13:00 | 23 | 0 | 0 |  | scheduled | Altman's AI Fears |
| _mgCjYQFwts | 2026-11-11T15:00 | 20 | 0 | 0 |  | scheduled | Pain in Capacity: Judging Entrepreneurs |
| O6lLBM4NsL8 | 2026-11-12T07:00 | 20 | 0 | 0 |  | scheduled | Startup Funding Fails in France |
| PcNq5VyrinA | 2026-11-12T09:30 | 23 | 0 | 0 |  | scheduled | Morning Cortisol Hack: Boost Your Day |
| 4YyRNIO-JAs | 2026-11-12T13:00 | 38 | 0 | 0 |  | scheduled | Attention's Addiction: Success Stories |

## Where to look in YouTube Studio (not in the API)
- **Viewed vs swiped away** (Shorts): Studio → Content → open a Short → Analytics → *Engagement* tab ("Viewed vs. swiped
  away"). The API has no such metric; `engagedViews ÷ views` above is the closest.
- **Copyright claims / restrictions**: Studio → Content → *Restrictions* column (the Data API doesn't show claims on
  your own videos).
- **Who else posted the same moment**: search YouTube for e.g. "Sam Altman Raj Shamani shorts" or "Bryson DeChambeau Figuring
  Out". If many channels posted the same clip before you, YouTube has little reason to show yours. (Not automated: each
  `search.list` call costs 100 quota units, which uploads need.)
