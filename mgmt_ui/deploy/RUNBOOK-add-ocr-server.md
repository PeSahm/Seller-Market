# Runbook: add a new OCR server to the pool (OCR high-availability)

The mgmt UI supports a **pool** of OCR endpoints (PR #155): the bots and the
verify-credentials flow read `ocr_service_url` as a **comma-separated list** and
fail over between them. To make OCR highly-available you add a second working
OCR endpoint so PouyanIt is no longer a single point of failure.

## ⚠️ The one hard requirement: an AVX2 CPU

**EasyOCR (PyTorch) needs AVX2 for its recognition network.** This was learned
the hard way (June 2026): the Tebyan VPSes are Ivy-Bridge Xeons with **AVX but
no AVX2**. They *import* torch and pass a blank-image test (which only runs the
*detection* network), but **crash the Python OCR process on a real captcha**
(the *recognition* network), returning HTTP 500. It is **not** fixable via env
vars (`DNNL_MAX_CPU_ISA=AVX` / `ATEN_CPU_CAPABILITY=avx` were tried).

So when buying/choosing the new server:

```bash
# Must print avx2 (and sse4_2). "Common KVM processor" (qemu64) = NO AVX at all.
grep -o -m1 -E 'avx512[a-z]*|avx2|avx|sse4_2|fma' /proc/cpuinfo | sort -u
```

Require **AVX2**, ≥ 2 GB free RAM (EasyOCR resident ≈ 600 MB), ≥ 12 GB free disk
(the OCR image is ~8.5 GB), and Iranian-fleet egress (so `ghcr-mirror.liara.ir`
works and the bots can reach it).

## Steps

1. **Provision the host** (per the Session-10 runbook): docker, the compose v2
   plugin, `daemon.json` with `registry-mirrors: ghcr-mirror.liara.ir` + Iranian
   DNS, Tehran time, and the mgmt UI's SSH key in `authorized_keys`. Add the
   server row in Admin → Servers (`image_pull_policy=never` if ghcr is blocked).

2. **Stage the OCR image:**
   ```bash
   docker pull ghcr-mirror.liara.ir/pesahm/ocr:latest   # or ghcr.io if reachable
   ```

3. **Seed the EasyOCR model** (the image tries to download it at runtime and
   stalls on the Iranian network). Copy the 94 MB model from PouyanIt:
   ```bash
   ssh root@5.10.248.55 'tar czf - -C /root/seller-market/easyocr_models \
       craft_mlt_25k.pth english_g2.pth' \
     | ssh <new-host> 'mkdir -p ~/easyocr_models && tar xzf - -C ~/easyocr_models'
   ```

4. **Run the OCR container** (the container runs as **root**, model at
   `/root/.EasyOCR/model`):
   ```bash
   docker run -d --name seller-market-ocr --restart unless-stopped \
     -p 18080:8080 \
     -v ~/easyocr_models:/root/.EasyOCR/model \
     ghcr-mirror.liara.ir/pesahm/ocr:latest
   # wait for: "EasyOCR model loaded and ready!"
   docker logs seller-market-ocr 2>&1 | grep -m1 "model loaded"
   ```

5. **VERIFY WITH A REAL CAPTCHA — not a blank image.** A blank/1×1 image only
   exercises *detection* and returns 200 even on a broken host. You MUST test the
   *recognition* path:
   ```bash
   # generate a digit image inside the container and decode it
   IMG=$(docker exec seller-market-ocr python3 -c "
   from PIL import Image, ImageDraw; import base64, io
   img=Image.new('RGB',(50,18),'white'); ImageDraw.Draw(img).text((3,3),'12345',fill='black')
   img=img.resize((250,90)); b=io.BytesIO(); img.save(b,format='PNG'); print(base64.b64encode(b.getvalue()).decode())")
   printf '{"base64":"%s"}' "$IMG" > /tmp/c.json
   curl -s -w ' [http %{http_code}]\n' -X POST http://127.0.0.1:18080/ocr/captcha-easy-base64 \
     -H 'Content-Type: application/json' --data @/tmp/c.json
   # PASS = http 200 with digits.  FAIL = http 500 (no AVX2 -> recognition crash).
   ```

6. **Check cross-host reachability** (so other hosts can fail over to it). Some
   Iranian providers block inbound on high ports (Tebyan blocks :18080 entirely;
   :80/:443 hit a filtering proxy). From another fleet host:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://<new-host>:18080/ocr/captcha-easy-base64 -d '{}'
   ```
   - **Reachable cross-host** → add `http://<new-host>:18080` to the pool (below).
   - **Local-only** → its own bots still use it via `host.docker.internal:18080`
     (the bot compose already renders `extra_hosts: host.docker.internal:host-gateway`);
     other hosts keep falling over to PouyanIt.

7. **Add it to the pool:** Admin → Settings → **OCR service URL(s)**, e.g.
   `http://host.docker.internal:18080, http://5.10.248.55:18080, http://<new-host>:18080`
   (`host.docker.internal:18080` = each host's own local OCR; the rest are the
   cross-host fallbacks). Save.

8. **Redeploy the stacks** so the bots pick up the new list (Admin → Load-balance
   → Rebalance / per-stack Redeploy, or the fleet redeploy). Verify a bot's env:
   ```bash
   docker exec <a-bot> printenv OCR_SERVICE_URL
   ```

Now two working OCR instances on two hosts → if one dies, captcha-solving
continues fleet-wide. That is real OCR HA.
