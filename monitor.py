from playwright.sync_api import sync_playwright
from datetime import date, timedelta
import json
import os
import requests

FILM_ID = "HO00000547"
SITE_ID = "IMAX"
START_DATE = date.today()
END_DATE = date(2026, 9, 14)
STATE_FILE = "last_state.json"
NOTIFY_URL = os.environ.get("NOTIFY_URL", "https://ntfy.sh/odyssey-tix-9f82kd")

# Minimum delay (ms) between requests so we're not hammering the site
REQUEST_DELAY_MS = 1500


def fetch_all_days():
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        captured = {}

        def handle_response(response):
            if "ocapi/v1/showtimes/by-business-date" in response.url:
                try:
                    captured[response.url] = response.json()
                except Exception:
                    pass

        page.on("response", handle_response)

        # Load the site once to establish a real session/auth context
        page.goto("https://web.imaxmelbourne.com.au/", wait_until="networkidle")

        d = START_DATE
        while d <= END_DATE:
            date_str = "first" if d == START_DATE else d.isoformat()
            url = (
                f"https://digital-api.imaxmelbourne.com.au/ocapi/v1/showtimes/"
                f"by-business-date/{date_str}?filmIds={FILM_ID}&siteIds={SITE_ID}"
            )
            try:
                page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception as e:
                print(f"Failed to load {d}: {e}")
                d += timedelta(days=1)
                continue

            if url in captured:
                results[d.isoformat()] = captured[url]
            else:
                print(f"No showtimes response captured for {d}")

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
            print(json.dumps(data, indent=2))
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
