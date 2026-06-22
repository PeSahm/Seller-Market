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

## exir / Rayan HamAfza — PENDING (no local khobregan creds; capture on a VPS)

Login `POST https://{tenant}.exirbroker.com/api/v2/login {username,password,captcha:<int>,otp:""}`.
Success marker = `nt` present (see EXIR_FINDINGS.md). On failure the body carries
`type=="error"` + a Persian `description`/`message`. Need a live probe to capture the
EXACT wrong-password description vs wrong-captcha description. Until captured, the exir
classifier stays conservative (always TRANSIENT → never auto-marks invalid).
TODO: run `cred_probe.py` with `EXIR_TENANT/EXIR_USER/EXIR_PASS` on a host that has
khobregan creds + broker + OCR reachability.
