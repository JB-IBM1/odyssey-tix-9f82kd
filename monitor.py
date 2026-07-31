from playwright.sync_api import sync_playwright
from datetime import date, timedelta
import json
import os
import requests

FILM_ID = "HO00000547"
SITE_ID = "IMAX"
START_DATE = date(2026, 8, 1)
END_DATE = date(2026, 9, 14)
STATE_FILE = "last_state.json"
NOTIFY_URL = os.environ.get("NOTIFY_URL", "https://ntfy.sh/odyssey-tix-9f82kd")

# Minimum delay (ms) between requests so we're not hammering the site
REQUEST_DELAY_MS = 1500


FILMS_PAGE_URL = f"https://web.imaxmelbourne.com.au/films/{FILM_ID}"


def fetch_all_days():
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        captured_headers = {}

        def handle_request(request):
            # Grab the auth header off the FIRST real showtimes request the
            # app itself makes - this is the one that carries a valid,
            # freshly-issued Authorization header we can't get by navigating
            # to the API URL ourselves.
            if (
                "ocapi/v1/showtimes/by-business-date" in request.url
                and not captured_headers
            ):
                captured_headers.update(request.headers)

        page.on("request", handle_request)

        # Load the real films page - this is what triggers the app's own
        # authenticated call to the showtimes endpoint for "today".
        page.goto(FILMS_PAGE_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)  # small buffer in case the call is slightly delayed

        if "authorization" not in {k.lower() for k in captured_headers}:
            browser.close()
            raise RuntimeError(
                "Could not capture an Authorization header from the films page. "
                "The page structure or app behaviour may have changed - "
                "re-check DevTools on " + FILMS_PAGE_URL
            )

        auth_header = next(
            v for k, v in captured_headers.items() if k.lower() == "authorization"
        )
        reuse_headers = {"Authorization": auth_header, "Accept": "application/json"}

        # context.request shares cookies/session with the page above (including
        # any Cloudflare cookies), so these calls look like the same session -
        # just lighter weight than a full page navigation per date.
        d = START_DATE
        while d <= END_DATE:
            date_str = "first" if d == START_DATE else d.isoformat()
            url = (
                f"https://digital-api.imaxmelbourne.com.au/ocapi/v1/showtimes/"
                f"by-business-date/{date_str}?filmIds={FILM_ID}&siteIds={SITE_ID}"
            )
            try:
                resp = context.request.get(url, headers=reuse_headers, timeout=20000)
                if resp.ok:
                    results[d.isoformat()] = resp.json()
                else:
                    print(f"{d}: HTTP {resp.status} - {resp.text()[:200]}")
            except Exception as e:
                print(f"Failed to fetch {d}: {e}")

            page.wait_for_timeout(REQUEST_DELAY_MS)
            d += timedelta(days=1)

        browser.close()

    return results


def load_last_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def notify(message, priority=None):
    try:
        headers = {"Title": "Odyssey ticket monitor"}
        if priority:
            headers["Priority"] = priority
        requests.post(
            NOTIFY_URL, data=message.encode("utf-8"), headers=headers, timeout=10
        )
    except Exception as e:
        print(f"Notify failed: {e}")


def main():
    last_state = load_last_state()
    new_state = {}
    increases = []

    try:
        all_days = fetch_all_days()
    except Exception as e:
        # Something broke badly enough that we couldn't even try each day
        # (browser launch failure, network error establishing the session, etc.)
        notify(f"Monitor run FAILED: {e}", priority="high")
        raise  # also fail the GitHub Actions run so it shows up as a red X

    if not all_days:
        # We looped through every day but captured zero responses - almost
        # certainly means auth/cookies broke or Cloudflare blocked the runner,
        # not that there's genuinely nothing to report.
        notify(
            "Monitor run captured NO showtime data for any day - "
            "likely an auth/blocking issue, not an empty schedule. Check the Actions log.",
            priority="high",
        )
    else:
        for day_str, data in all_days.items():
            # Uncomment once to confirm the real field names for your response shape:
            # print(json.dumps(data, indent=2))
            sessions = data.get("Showtimes", data.get("showtimes", []))
            for session in sessions:
                session_id = session.get("Id") or session.get("id")
                seats_now = session.get("SeatsAvailable", session.get("seats_available"))
                key = f"{day_str}:{session_id}"
                new_state[key] = seats_now

                seats_before = last_state.get(key)
                # Only notify when seats went UP - a drop just means someone
                # else booked, which isn't actionable for you.
                if (
                    seats_before is not None
                    and seats_now is not None
                    and seats_now > seats_before
                ):
                    increases.append(
                        f"{day_str} session {session_id}: {seats_before} -> {seats_now} seats"
                    )

        if increases:
            notify("Odyssey seats opened up:\n" + "\n".join(increases), priority="high")
            print("\n".join(increases))
        else:
            print("No new seats detected.")

        save_state(new_state)


if __name__ == "__main__":
    main()
