# San Diego Fun Finder First-Party Analytics Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add privacy-conscious first-party analytics to the public San Diego Fun Finder so Roberto can measure visitors, popular behaviors, outbound event clicks, and filter usage without depending on a third-party analytics vendor.

**Architecture:** Use a first-party `POST /api/analytics` event collector in Flask, backed by a local SQLite database via Python stdlib `sqlite3`. Add a protected private analytics summary route and mobile-friendly dashboard page that reads aggregated metrics from the same store. Client-side tracking will use anonymous browser session IDs in `localStorage` plus `navigator.sendBeacon()`/`fetch(..., {keepalive:true})` for resilient delivery.

**Tech Stack:** Flask 3, stdlib `sqlite3`, stdlib `unittest`, inline browser JavaScript, static HTML dashboard, Render web service.

---

### Task 1: Add failing backend tests for analytics storage and aggregation

**Objective:** Define expected analytics behavior before writing any production code.

**Files:**
- Create: `tests/test_analytics.py`
- Create: `tests/__init__.py`

**Step 1: Write failing tests**
- Verify anonymous events can be stored in a SQLite DB.
- Verify summaries compute page views, unique sessions, outbound clicks, top filters, top sources, and daily trends.
- Verify invalid events are rejected.

**Step 2: Run test to verify failure**

Run: `python3 -m unittest -v tests.test_analytics`

Expected: FAIL because the analytics module does not exist yet.

### Task 2: Implement analytics storage module

**Objective:** Build the minimum backend storage layer that makes the tests pass.

**Files:**
- Create: `analytics.py`

**Step 1: Implement schema and insert helpers**
- Use a single `analytics_events` table.
- Store only anonymous session IDs and event metadata; do not store raw IPs.
- Add validation and max-length trimming.

**Step 2: Implement summary aggregation**
- Aggregate in Python from recent rows to keep SQL simple.
- Return totals, trend rows, top filters, top sources, top outbound titles, and referrers.

**Step 3: Run tests**

Run: `python3 -m unittest -v tests.test_analytics`

Expected: PASS.

### Task 3: Add failing Flask endpoint tests

**Objective:** Lock in API behavior before wiring app routes.

**Files:**
- Create: `tests/test_app_analytics_routes.py`
- Modify: `app.py`

**Step 1: Write failing tests**
- Verify `POST /api/analytics` accepts a valid event and returns success JSON.
- Verify `GET /api/analytics/summary` rejects access without a configured key.
- Verify `GET /api/analytics/summary` returns summary JSON when `X-Analytics-Key` is supplied.
- Verify `GET /analytics` serves the private dashboard shell without putting secrets in the URL.

**Step 2: Run tests to verify failure**

Run: `python3 -m unittest -v tests.test_app_analytics_routes`

Expected: FAIL because the routes do not exist yet.

### Task 4: Implement analytics routes and security boundary updates

**Objective:** Add the protected analytics API and dashboard route.

**Files:**
- Modify: `app.py`
- Modify: `README.md`
- Create: `analytics_dashboard.html`

**Step 1: Add routes**
- `POST /api/analytics` for event collection.
- `GET /api/analytics/summary` protected by `FUN_FINDER_ANALYTICS_KEY`.
- `GET /analytics` serves the shell; the private summary fetch is protected by the same key via header auth.

**Step 2: Add private dashboard page**
- Mobile-first layout.
- KPI cards and short briefing-style sections.
- No sticky mobile top section.

**Step 3: Run route tests**

Run: `python3 -m unittest -v tests.test_app_analytics_routes`

Expected: PASS.

### Task 5: Add failing frontend tracking tests or targeted verification harness

**Objective:** Define the most important client-tracking behaviors before editing the UI.

**Files:**
- Create: `tests/test_frontend_analytics_hooks.py` (string/route checks only)
- Modify: `public_dashboard.html`

**Step 1: Write failing checks**
- Verify the dashboard contains an analytics session helper.
- Verify tracking calls exist for page views, filter changes, save/clear profile, source modal opens, day selection, refresh, and outbound event clicks.

**Step 2: Run tests to verify failure**

Run: `python3 -m unittest -v tests.test_frontend_analytics_hooks`

Expected: FAIL because the hooks are not present yet.

### Task 6: Implement client analytics hooks

**Objective:** Capture the behaviors Roberto actually cares about.

**Files:**
- Modify: `public_dashboard.html`

**Step 1: Add anonymous session and tracking helpers**
- LocalStorage session ID.
- `sendBeacon` primary path with `fetch` fallback.
- Respect `Do Not Track` when enabled.

**Step 2: Hook important user actions**
- `page_view`
- `refresh_clicked`
- `filter_changed`
- `day_selected`
- `save_profile_clicked`
- `clear_profile_clicked`
- `sources_modal_opened`
- `event_detail_clicked`
- `no_results_seen`

**Step 3: Run frontend hook tests**

Run: `python3 -m unittest -v tests.test_frontend_analytics_hooks`

Expected: PASS.

### Task 7: End-to-end verification

**Objective:** Prove the feature works with real runtime behavior.

**Files:**
- Reuse existing files.

**Step 1: Run the app locally with an analytics key**

Run:
```bash
FUN_FINDER_ANALYTICS_KEY=dev-analytics python3 app.py
```

**Step 2: Exercise the app**
- Load `/`
- Change filters
- Open sources
- Click event detail links
- Visit `/analytics`, enter `dev-analytics` in the key prompt, and confirm the page loads data via header auth

**Step 3: Verify results**
- Confirm analytics rows exist.
- Confirm summary JSON and dashboard reflect real interactions.

### Task 8: Ship and document caveats

**Objective:** Commit the finished feature and clearly document deployment constraints.

**Files:**
- Modify: `README.md`

**Step 1: Document caveat**
- Render free web services do not have persistent disks, so local SQLite analytics can reset on restart/redeploy.
- Recommend future upgrade path: persistent disk or managed database.

**Step 2: Commit and push**

Run:
```bash
git add analytics.py analytics_dashboard.html public_dashboard.html app.py README.md tests docs/plans
git commit -m "feat: add first-party fun finder analytics"
git push origin main
```

---

## Verification checklist
- [ ] All new tests were written before the production code they cover.
- [ ] Backend storage tests pass.
- [ ] Route tests pass.
- [ ] Frontend tracking checks pass.
- [ ] Local end-to-end verification captured real analytics events.
- [ ] README explains the security boundary and Render persistence caveat.
- [ ] Analytics dashboard works on a phone-sized viewport.
