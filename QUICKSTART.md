# uDispute — Quick Terminal Commands

## Start the app
```
cd ~/Desktop/training.py
python _run_dev.py
```
App runs at: http://127.0.0.1:5001

## Start ngrok (separate terminal)
```
ngrok http 5001
```
Copy the `https://______.ngrok-free.app` URL — that's your public address.

## Update APP_BASE_URL for mailing
After starting ngrok, update `.env`:
```
APP_BASE_URL=https://your-ngrok-url.ngrok-free.app
```
Then restart the app (`Ctrl+C` then `python _run_dev.py` again).

## Reset onboarding tours (browser console)
```
GlassTour.reset()
```
Then refresh the page.

## Beta invite codes
- `UDISPUTE2026`
- `EARLYACCESS`
- `GLASSGANG`
- `LIQUIDGLASS`
- `CREDITFIX`
- `SKOOLBETA`
- `FIRSTROUND`
- `UPOWER`
- `BETAWAVE`
- `REPAIRMODE`
