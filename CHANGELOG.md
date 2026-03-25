# Changelog

## [2025-03-25] Security Hardening

### Added
- **CSRF protection** on all forms via Flask-WTF (`CSRFProtect`). CSRF meta tag + fetch interceptor in `base_header.html` automatically covers AJAX calls. Pipeline API blueprint exempted (JSON-only, no HTML forms).
- **Rate limiting** on signup (5/min) and login (10/min) via Flask-Limiter with graceful fallback if not installed.
- **Security headers** on all responses: `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`, `Strict-Transport-Security` (HTTPS only).
- **Audit logging** for security events: signup attempts, login success/failure, payment intents, bad beta codes.
- **Session hardening**: 60-minute timeout, `HttpOnly` + `SameSite=Lax` cookies, `Secure` flag in production.
- **Open redirect protection** on login `next` parameter — validates redirect stays on-domain.
- **Password minimum length** (8 characters) enforced on signup.
- **50 MB upload limit** (`MAX_CONTENT_LENGTH`).
- **`.env.example`** updated with all current environment variables and generation instructions.

### Changed
- **`SECRET_KEY`** no longer defaults to `'smartflow'`. Required in production (crashes on startup without it). Auto-generates ephemeral key for local dev only.
- **`debug=True`** removed from production — now conditional on `FLASK_ENV=development`.
- **Stripe `create_payment_intent`** enforces server-side pricing ($80 Pro / $125 Business). Client-sent `amount` is ignored. Added idempotency key.
- **Dev plan-switch route** (`/dev-switch/<plan>`) now returns 404 in production.
- **Cloudinary file proxy** replaced with redirect — eliminates SSRF vector. Only `res.cloudinary.com` domains allowed.
- **`requirements.txt`** pinned all dependency versions. Added `flask-wtf` and `flask-limiter`.

### Removed
- `services/pii_redactor.py` — removed; dispute letters require full account numbers to be legally valid.

### Security Fixes
- SSRF via server-side Cloudinary proxy (business file viewer + dispute uploads)
- Open redirect on post-login `next` parameter
- Client-controlled Stripe payment amounts
- Hardcoded default secret key
- Debug mode always on
- No CSRF protection
- No rate limiting on auth endpoints
- No session expiration
