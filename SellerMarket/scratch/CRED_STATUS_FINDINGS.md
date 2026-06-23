# Credential-status markers — wrong-password vs wrong-captcha (live probe, 2026-06-23)

> Step 0 of the credential-verification feature. Captured by `scratch/cred_probe.py`
> (read-only, no orders; secrets/PII never printed — only diagnostic marker fields).
> Account ids masked. The classifier keys on these markers to decide
> VALID / INVALID_CREDENTIALS / TRANSIENT. **Conservative rule: only the explicit
> invalid-credentials marker → INVALID; everything else → TRANSIENT (keep retrying).**

## ephoenix family + ibtrader — DISCRIMINATOR = top-level `errorCode` (numeric, language-independent)

Login `POST https://identity{-code|.ibtrader}.{ephoenix.ir}/api/v2/accounts/login`
returns **HTTP 200 in every case**; the body's `errorCode` distinguishes them:

| case | `errorCode` | `isSuccess` | `token` | body keys |
|---|---|---|---|---|
| **VALID** (good captcha + good password) | `0` | true | present | full set (token, sessionId, expireIn, …) |
| **INVALID CAPTCHA** (OCR misread) | `-1000` | false | — | short: `[errorCode, errorMessage, isSuccess]` |
| **INVALID CREDENTIALS** (wrong password) | `3000` | false | null | full set (token key present but null) |

Confirmed identical on **`bbi`** (ephoenix) and **`ib`** (ibtrader). Robustness proof:
in the wrong-password run, the attempts where OCR misread the captcha returned
`-1000` while the correctly-solved ones returned `3000` — so the two are cleanly
separable on `errorCode` and never conflated.

**Classifier rule (ephoenix + ib):**
- `errorCode == 3000` → **INVALID_CREDENTIALS** (skip / block).
- `errorCode == -1000` → invalid captcha → **TRANSIENT** (retry a fresh captcha — today's behavior).
- `errorCode == 0` and `token` present → **VALID**.
- any other / missing `errorCode`, non-200, non-JSON, transport error → **TRANSIENT** (conservative).

`errorMessage` carries a Persian string too, but we key on the numeric `errorCode`
(language-independent, stable). The bot's `api_client._login_with_captcha` does
`raise_for_status()` — but login is HTTP 200 here, so the body is available before any raise.

## exir / Rayan HamAfza — DISCRIMINATOR = top-level `errorCode` (numeric, language-independent)

Login `POST https://{tenant}.exirbroker.com/api/v2/login {username,password,captcha:<int>,otp:""}`.
Captured live on khobregan (2026-06-23):

| case | HTTP | `errorCode` | `type` | description (Persian) |
|---|---|---|---|---|
| **VALID** (good captcha + good password) | 200 | — | — | (`nt` present) |
| **INVALID CAPTCHA** | 401 | `9002` | `error` | "خطا:‌ کد امنیتی وارد شده صحیح نیست" |
| **INVALID CREDENTIALS** (wrong password) | 403 | `40037` | `error` | "نام کاربری یا کلمه عبور اشتباه است " |

Robustness proof: in the wrong-password run, an OCR-misread attempt returned `9002`
while the correctly-solved one returned `40037` — cleanly separable on `errorCode`.

**Classifier rule (exir):**
- `errorCode == 40037` → **INVALID_CREDENTIALS** (skip / block).
- `errorCode == 9002` (bad captcha) and every other code / non-error → **TRANSIENT** (retry).
- `nt` present → **VALID**.

We key on the numeric `errorCode` (not the Persian `description`, which has a trailing
space + yeh-spelling variants). The body also carries `descriptionEn` if ever needed.
