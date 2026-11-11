# Clinic booking

A booking page and JSON API for the [Koya assessment](https://koyaacademy.com/assessment-api). Flask serves both from one origin. SQLite stores slots, bookings, replay records and rate-limit events. The API base URL is the site origin and the booking page is at `/`.

## Run locally

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export BOOKING_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
gunicorn --bind 127.0.0.1:8000 app:app
```

In another terminal, set the same key and run `python3 seed.py` once. It replaces all slots and clears bookings. Open `http://127.0.0.1:8000` and enter the key to load slots.

## Deployment

Run `gunicorn --bind 0.0.0.0:$PORT app:app` behind the host's HTTPS endpoint. Configure `BOOKING_API_KEY` as a runtime secret and `BOOKING_DATABASE` as a file path on a persistent volume. Use one service instance with the volume mounted. After deploying, set `BOOKING_BASE_URL` to the HTTPS origin and run `seed.py` once. Enter the same origin as the API base and web page URL on the assessment form.

The shared assessment key grants access to every API endpoint, including reset. Use synthetic patient information in the demo.

## API behavior

Every API route expects `X-API-Key`. Booking writes use a transaction and a partial unique index to prevent simultaneous confirmations for the same slot. Cancellation releases the slot. Matching idempotent replays return the original response and skip rate limiting. New attempts count toward a five-per-minute rolling limit per normalized email. Booking history uses an insertion-order cursor.
