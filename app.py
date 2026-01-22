#!/usr/bin/env python3
import os
import math
import html
import base64
import textwrap
import asyncio
import datetime as dt
from typing import Dict, Any, Optional, Tuple, List

import pandas as pd
import streamlit as st
import httpx
import streamlit.components.v1 as components
from solders.pubkey import Pubkey

# ============================
# CONFIG
# ============================
DATA_FILE = "token_list_with_images.csv"   # must be next to app.py
PAGE_SIZE = 100
COLS = 10

# Hardcoded Helius key (as requested). You can override via env var HELIUS_API_KEY.
HELIUS_API_KEY = "2314091d-cb57-454c-8ae0-85d261c91824"
HELIUS_RPC_BASE = "https://mainnet.helius-rpc.com/"
HELIUS_TIMEOUT_S = 12.0

# Optional: override RPC used for on-chain Metaplex metadata reads
SOLANA_RPC_URL = (os.getenv("SOLANA_RPC_URL") or "").strip() or f"{HELIUS_RPC_BASE}?api-key={os.getenv('HELIUS_API_KEY','').strip() or HELIUS_API_KEY}"

# Metaplex Token Metadata program
METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")

# Special-case replacements (local images next to app.py)
REPLACEMENTS = [
    ("rs.debot.ai/logo/DrZ26cKJDksVRWib3DVVsjo9eeXccc7hKhDJviiYEEZY.png", "bird.png"),
    ("rs.debot.ai/logo/4NBTf8PfLH4oLFnwf3knv46FY9i5oXjDxffCetXRpump.png", "4NBT.png"),
]
LOCAL_SENT_PREFIX = "__LOCAL__:"  # sentinel used internally

# ============================
# STYLE (YZY vibe)
# ============================
CSS = """
<style>
.main .block-container { max-width: 1320px; padding-top: 18px; }
body { background: #ffffff; }
.yzy-title { text-align:center; margin-top: 6px; }
.yzy-title h1 { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Arial; letter-spacing: .18em; font-weight: 800; margin: 0; }
.yzy-title h2 { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Arial; letter-spacing: .14em; font-weight: 600; font-size: 12px; margin: 6px 0 0 0; color:#111; }
.smallcap { color:#666; font-size: 12px; text-align:left; margin: 10px 0 12px 2px; }

.token-card { border: 1px solid #d9d9d9; border-radius: 14px; padding: 10px 10px 12px 10px; background: #fff; }
.token-top { display:flex; gap:8px; align-items:flex-start; justify-content:space-between; }
.token-sym { font-weight: 800; letter-spacing: .06em; font-size: 12px; color: #111; line-height: 1.1; }
.token-name { font-size: 11px; color:#444; margin-top: 2px; line-height: 1.15; overflow:hidden; text-overflow: ellipsis; white-space: nowrap; }

.token-img { margin-top: 8px; width: 100%; height: 116px; border: 1px solid #ededed; border-radius: 10px; display:flex; align-items:center; justify-content:center; overflow:hidden; background: #fafafa; }
.token-img img { width:100%; height:100%; object-fit: contain; }
.noimg { font-size: 10px; color: #666; padding: 8px; text-align:center; line-height: 1.2; }

.token-meta { margin-top: 8px; font-size: 10px; color:#333; line-height: 1.2; }
.token-ca { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
.token-date { color:#666; margin-top: 3px; }

.copybtn-wrap { margin-top: 8px; }
</style>
"""

# ============================
# HELPERS
# ============================
def short_ca(ca: str) -> str:
    ca = (ca or "").strip()
    if len(ca) <= 10:
        return ca
    return f"{ca[:4]}…{ca[-4:]}"

def nonempty_str(x) -> Optional[str]:
    if isinstance(x, str):
        t = x.strip()
        if t and t not in ("0", "null", "None", "none"):
            return t
    return None

@st.cache_data(show_spinner=False)
def load_local_image_b64(filename: str) -> Optional[str]:
    try:
        with open(filename, "rb") as f:
            b = f.read()
        ext = filename.lower().split(".")[-1]
        mime = "image/png"
        if ext in ("jpg", "jpeg"):
            mime = "image/jpeg"
        elif ext == "webp":
            mime = "image/webp"
        data = base64.b64encode(b).decode("ascii")
        return f"data:{mime};base64,{data}"
    except Exception:
        return None

@st.cache_data(show_spinner=False)
def local_images_map() -> Dict[str, Optional[str]]:
    m = {}
    for _, fn in REPLACEMENTS:
        m[fn] = load_local_image_b64(fn)
    return m

def apply_replacements(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = str(url).strip()
    low = u.lower()
    for substr, local_fn in REPLACEMENTS:
        if substr.lower() in low:
            return f"{LOCAL_SENT_PREFIX}{local_fn}"
    return u

def normalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = str(url).strip()
    if not u:
        return None

    u = apply_replacements(u) or u
    if u.startswith(LOCAL_SENT_PREFIX):
        return u

    low = u.lower()

    if "cdn-cgi/image" in low:
        last_https = low.rfind("https://")
        last_http = low.rfind("http://")
        last = max(last_https, last_http)
        if last != -1:
            u = u[last:]

    if u.startswith("//"):
        u = "https:" + u

    if u.lower().startswith("ipfs://"):
        rest = u[7:].lstrip("/")
        if rest.lower().startswith("ipfs/"):
            rest = rest[5:]
        u = "https://ipfs.io/ipfs/" + rest

    # cf-ipfs.com gateway -> normalize to ipfs.io (more reliable in browsers)
    low = u.lower()
    if low.startswith("https://cf-ipfs.com/ipfs/") or low.startswith("http://cf-ipfs.com/ipfs/"):
        cid = u.split("/ipfs/", 1)[1]
        u = "https://ipfs.io/ipfs/" + cid
    elif low.startswith("https://cf-ipfs.com/ipns/") or low.startswith("http://cf-ipfs.com/ipns/"):
        name = u.split("/ipns/", 1)[1]
        u = "https://ipfs.io/ipns/" + name


    low = u.lower()
    if "rs.debot.ai/" in low or "debot.ai/" in low:
        u = u.split("?", 1)[0]

    u = apply_replacements(u) or u
    return u

def is_local_sent(u: Optional[str]) -> bool:
    return bool(u) and str(u).startswith(LOCAL_SENT_PREFIX)

def local_sent_to_data_uri(u: str, local_map: Dict[str, Optional[str]]) -> Optional[str]:
    fn = u[len(LOCAL_SENT_PREFIX):]
    return local_map.get(fn)

def helius_url() -> str:
    key = (os.getenv("HELIUS_API_KEY") or "").strip() or HELIUS_API_KEY
    return f"{HELIUS_RPC_BASE}?api-key={key}"

def _extract_image_from_json(j: Dict[str, Any]) -> Optional[str]:
    for k in ("image", "image_url", "imageUrl", "logoURI", "logoUri", "animation_url", "animationUrl"):
        v = j.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    ext = j.get("extensions")
    if isinstance(ext, dict):
        v = ext.get("image")
        if isinstance(v, str) and v.strip():
            return v.strip()
    props = j.get("properties")
    if isinstance(props, dict):
        files = props.get("files")
        if isinstance(files, list):
            for f in files:
                if isinstance(f, dict):
                    v = f.get("uri") or f.get("url")
                    if isinstance(v, str) and v.strip():
                        return v.strip()
    return None

# ============================
# HELIUS FALLBACK
# ============================
def _extract_helius_image(asset: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    content = asset.get("content") or {}
    json_uri = None
    if isinstance(content, dict):
        files = content.get("files")
        if isinstance(files, list) and files:
            f0 = files[0] if isinstance(files[0], dict) else None
            if isinstance(f0, dict):
                img = nonempty_str(f0.get("cdn_uri")) or nonempty_str(f0.get("uri"))
                img = normalize_url(img)
                if img:
                    return img, None
        links = content.get("links")
        if isinstance(links, dict):
            img = normalize_url(nonempty_str(links.get("image")))
            if img:
                return img, None
        json_uri = normalize_url(nonempty_str(content.get("json_uri")))
    if json_uri:
        return None, "Helius: no direct image, has json_uri"
    return None, "Helius: asset has no image fields"

@st.cache_data(show_spinner=False, ttl=3600)
def helius_get_asset_batch(mints: List[str]) -> Dict[str, Dict[str, Any]]:
    if not mints:
        return {}
    url = helius_url()
    out: Dict[str, Dict[str, Any]] = {}
    headers = {"Content-Type": "application/json"}

    with httpx.Client(timeout=HELIUS_TIMEOUT_S, follow_redirects=True) as client:
        for i in range(0, len(mints), 100):
            ids = mints[i:i + 100]
            payload = {"jsonrpc": "2.0", "id": "1", "method": "getAssetBatch", "params": {"ids": ids}}
            try:
                r = client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
                assets = data.get("result")
                if not isinstance(assets, list):
                    continue

                json_needed: List[Tuple[str, str]] = []
                for a in assets:
                    if not isinstance(a, dict):
                        continue
                    mint = a.get("id")
                    if not isinstance(mint, str):
                        continue
                    img, reason = _extract_helius_image(a)
                    content = a.get("content") or {}
                    json_uri = normalize_url(nonempty_str(content.get("json_uri"))) if isinstance(content, dict) else None
                    out[mint] = {"image": img, "json_uri": json_uri, "reason": reason}
                    if (not img) and json_uri:
                        json_needed.append((mint, json_uri))

                for mint, jurl in json_needed:
                    try:
                        jr = client.get(jurl, headers={"Accept": "application/json,*/*"})
                        jr.raise_for_status()
                        ctype = (jr.headers.get("content-type") or "").lower()
                        if ctype.startswith("image/"):
                            out[mint]["image"] = normalize_url(jurl)
                            out[mint]["reason"] = None
                            continue
                        if "text/html" in ctype or "<html" in jr.text[:200].lower():
                            continue
                        jj = jr.json()
                        img2 = normalize_url(_extract_image_from_json(jj))
                        if img2:
                            out[mint]["image"] = img2
                            out[mint]["reason"] = None
                    except Exception:
                        continue

            except Exception as e:
                for mint in ids:
                    out[mint] = {"image": None, "json_uri": None, "reason": f"Helius error: {type(e).__name__}"}

    return out

# ============================
# METAPLEX (ON-CHAIN) FALLBACK
# ============================
def metadata_pda_for_mint(mint_str: str) -> Pubkey:
    mint = Pubkey.from_string(mint_str)
    seeds = [b"metadata", bytes(METADATA_PROGRAM_ID), bytes(mint)]
    pda, _ = Pubkey.find_program_address(seeds, METADATA_PROGRAM_ID)
    return pda

def _extract_first_uri_from_bytes(b: bytes) -> Optional[str]:
    needles = [b"https://", b"http://", b"ipfs://"]
    idx = -1
    for n in needles:
        i = b.find(n)
        if i != -1 and (idx == -1 or i < idx):
            idx = i
    if idx == -1:
        return None
    end = b.find(b"\x00", idx)
    if end == -1:
        end = min(len(b), idx + 400)
    raw = b[idx:end]
    try:
        s = raw.decode("utf-8", errors="ignore").strip()
        s = s.split()[0]
        return s
    except Exception:
        return None

async def _rpc_get_account_data_base64(client: httpx.AsyncClient, pubkey: str) -> Optional[bytes]:
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "getAccountInfo",
        "params": [pubkey, {"encoding": "base64"}],
    }
    r = await client.post(SOLANA_RPC_URL, json=payload)
    r.raise_for_status()
    j = r.json()
    v = (j.get("result") or {}).get("value")
    if not v:
        return None
    data = v.get("data")
    if not (isinstance(data, list) and len(data) >= 1 and isinstance(data[0], str)):
        return None
    return base64.b64decode(data[0])

async def _fetch_json_or_image(client: httpx.AsyncClient, uri: str) -> Tuple[Optional[str], Optional[str]]:
    u = normalize_url(uri)
    if not u:
        return None, "Metaplex: empty uri"
    try:
        r = await client.get(u, headers={"Accept": "application/json,*/*"})
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if ctype.startswith("image/"):
            return normalize_url(u), None
        txt_head = (r.text or "")[:200].lower()
        if "text/html" in ctype or "<html" in txt_head:
            return None, "Metaplex: token URI returned HTML"
        jj = r.json()
        img = normalize_url(_extract_image_from_json(jj))
        if img:
            return img, None
        return None, "Metaplex: JSON has no image field"
    except Exception as e:
        return None, f"Metaplex: failed to fetch token JSON ({type(e).__name__})"

@st.cache_data(show_spinner=False, ttl=3600)
def metaplex_resolve_images(mints: List[str]) -> Dict[str, Dict[str, Any]]:
    if not mints:
        return {}

    async def run() -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        sem = asyncio.Semaphore(18)
        async with httpx.AsyncClient(timeout=HELIUS_TIMEOUT_S, follow_redirects=True) as client:
            async def one(m: str):
                async with sem:
                    try:
                        pda = metadata_pda_for_mint(m)
                        b = await _rpc_get_account_data_base64(client, str(pda))
                        if not b:
                            out[m] = {"image": None, "reason": "Metaplex: no metadata account"}
                            return
                        uri = _extract_first_uri_from_bytes(b)
                        if not uri:
                            out[m] = {"image": None, "reason": "Metaplex: no token URI in metadata bytes"}
                            return
                        img, reason = await _fetch_json_or_image(client, uri)
                        if img:
                            out[m] = {"image": img, "reason": None}
                        else:
                            out[m] = {"image": None, "reason": reason or "Metaplex: no image"}
                    except Exception as e:
                        out[m] = {"image": None, "reason": f"Metaplex error: {type(e).__name__}"}

            await asyncio.gather(*[one(m) for m in mints])
        return out

    try:
        return asyncio.run(run())
    except RuntimeError:
        # event loop already running -> fallback sequential
        out: Dict[str, Dict[str, Any]] = {}
        with httpx.Client(timeout=HELIUS_TIMEOUT_S, follow_redirects=True) as c:
            for m in mints:
                try:
                    pda = metadata_pda_for_mint(m)
                    payload = {"jsonrpc":"2.0","id":"1","method":"getAccountInfo","params":[str(pda),{"encoding":"base64"}]}
                    r = c.post(SOLANA_RPC_URL, json=payload)
                    r.raise_for_status()
                    j = r.json()
                    v = (j.get("result") or {}).get("value")
                    if not v:
                        out[m] = {"image": None, "reason": "Metaplex: no metadata account"}
                        continue
                    data = v.get("data")
                    if not (isinstance(data, list) and isinstance(data[0], str)):
                        out[m] = {"image": None, "reason": "Metaplex: bad account data"}
                        continue
                    b = base64.b64decode(data[0])
                    uri = _extract_first_uri_from_bytes(b)
                    if not uri:
                        out[m] = {"image": None, "reason": "Metaplex: no token URI in metadata bytes"}
                        continue
                    u = normalize_url(uri)
                    rr = c.get(u, headers={"Accept":"application/json,*/*"})
                    rr.raise_for_status()
                    ctype = (rr.headers.get("content-type") or "").lower()
                    if ctype.startswith("image/"):
                        out[m] = {"image": normalize_url(u), "reason": None}
                        continue
                    if "text/html" in ctype or "<html" in (rr.text or "")[:200].lower():
                        out[m] = {"image": None, "reason": "Metaplex: token URI returned HTML"}
                        continue
                    jj = rr.json()
                    img = normalize_url(_extract_image_from_json(jj))
                    if img:
                        out[m] = {"image": img, "reason": None}
                    else:
                        out[m] = {"image": None, "reason": "Metaplex: JSON has no image field"}
                except Exception as e:
                    out[m] = {"image": None, "reason": f"Metaplex error: {type(e).__name__}"}
        return out

# ============================
# DATA
# ============================
@st.cache_data(show_spinner=False)
def load_tokens(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    needed = ["symbol", "name", "ca", "mint_time", "image_url"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise RuntimeError(f"CSV missing columns {missing}. Found: {df.columns.tolist()}")

    out = df[needed].copy()
    out["ca"] = out["ca"].astype(str).str.strip()
    out["symbol"] = out["symbol"].astype(str).fillna("").str.strip()
    out["name"] = out["name"].astype(str).fillna("").str.strip()
    out["mint_time"] = out["mint_time"].astype(str).fillna("").str.strip()

    out["image_url"] = out["image_url"].astype(str).where(df["image_url"].notna(), "").str.strip()
    out["image_url"] = out["image_url"].apply(lambda x: normalize_url(nonempty_str(x)) or "")

    out["mint_dt"] = pd.to_datetime(out["mint_time"], errors="coerce", utc=False)
    out["mint_date"] = out["mint_dt"].dt.date

    out = out[out["ca"].str.len() > 0].drop_duplicates(subset=["ca"], keep="first").reset_index(drop=True)
    return out

# ============================
# COPY BUTTON (no rerun)
# ============================
def copy_button_html(text: str, key: str) -> str:
    btn_id = f"btn_{key}"
    safe_text_js = text.replace("\\", "\\\\").replace('"', '\\"')
    return f"""
<div class="copybtn-wrap">
  <button id="{btn_id}" style="
    border-radius:999px; border:1px solid #d9d9d9; background:#fff; color:#111;
    font-size:11px; padding:4px 10px; cursor:pointer;">
    Copy CA
  </button>
  <span id="{btn_id}_msg" style="font-size:11px; color:#666; margin-left:8px;"></span>
</div>
<script>
  const btn = document.getElementById("{btn_id}");
  const msg = document.getElementById("{btn_id}_msg");
  btn.addEventListener("click", async () => {{
    try {{
      await navigator.clipboard.writeText("{safe_text_js}");
      msg.textContent = "Copied ✓";
      setTimeout(()=>{{ msg.textContent=""; }}, 1200);
    }} catch (e) {{
      msg.textContent = "Copy blocked";
      setTimeout(()=>{{ msg.textContent=""; }}, 1500);
    }}
  }});
</script>
"""

def image_box_html(img_url: Optional[str], reason: str, key: str, local_map: Dict[str, Optional[str]]) -> str:
    safe_reason = html.escape(reason)
    url_to_use = img_url
    if img_url and is_local_sent(img_url):
        data_uri = local_sent_to_data_uri(img_url, local_map)
        url_to_use = data_uri if data_uri else None

    if url_to_use:
        safe_url = html.escape(url_to_use)
        return f"""
<div class="token-img" id="imgwrap_{key}">
  <img src="{safe_url}" loading="lazy"
       onerror="this.onerror=null; const p=this.parentElement; p.innerHTML='<div class=\\'noimg\\'>{safe_reason}</div>';" />
</div>
"""
    return f'<div class="token-img"><div class="noimg">{safe_reason}</div></div>'

# ============================
# APP
# ============================
st.set_page_config(page_title="YZY-TKNS", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
st.markdown('<div class="yzy-title"><h1>YZY TKNS</h1><h2>THE ARCHIVE</h2></div>', unsafe_allow_html=True)

local_map = local_images_map()

try:
    df = load_tokens(DATA_FILE)
except Exception as e:
    st.error(f"Couldn't load {DATA_FILE}. Put it next to app.py. Error: {e}")
    st.stop()
# Read optional date from URL query params (e.g. ?date=2026-01-01)
_default_date = None
try:
    _qp = st.query_params
    _qdate = _qp.get("date")
    if isinstance(_qdate, list):
        _qdate = _qdate[0] if _qdate else None
except Exception:
    _qp = st.experimental_get_query_params()  # type: ignore[attr-defined]
    _qdate = (_qp.get("date") or [None])[0]
if _qdate:
    try:
        _default_date = dt.date.fromisoformat(str(_qdate))
    except Exception:
        _default_date = None

archive_tab, stats_tab, chain_tab = st.tabs(["Archive", "Statistic", "YZY BLOCKCHAIN"])

with archive_tab:
    cA, cB, cC, cD = st.columns([2.2, 2.2, 3.2, 2.4])
    with cA:
        sort_mode = st.selectbox("Sort", options=["Newest", "Oldest"], index=0)
    with cB:
        show_only_images = st.checkbox("Show only tokens with images", value=False)
    with cC:
        picked_date = st.date_input("Mint date (optional)", value=_default_date, help="Pick a day to show tokens minted that day.", key="mint_date_picker")
    with cD:
        page = st.number_input("Page", min_value=1, max_value=999999, value=1, step=1)

    # If user cleared the date picker that was prefilled from URL, drop the query param so it doesn't come back.
    if _qdate and picked_date is None:
        try:
            st.query_params.pop("date", None)
        except Exception:
            st.experimental_set_query_params()  # type: ignore[attr-defined]
        st.rerun()

    search_q = st.text_input("Search (symbol / name / CA)", value="", placeholder="Search token…")

    f = df.copy()
    if search_q.strip():
        q = search_q.strip().lower()
        f = f[
            f["symbol"].str.lower().str.contains(q)
            | f["name"].str.lower().str.contains(q)
            | f["ca"].str.lower().str.contains(q)
        ]
    if picked_date is not None:
        f = f[f["mint_date"] == picked_date]
    if show_only_images:
        f = f[f["image_url"].astype(str).str.strip() != ""]

    if sort_mode == "Newest":
        f = f.sort_values(by=["mint_dt", "mint_time"], ascending=False)
    else:
        f = f.sort_values(by=["mint_dt", "mint_time"], ascending=True)

    f = f.reset_index(drop=True)
    total = len(f)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(int(page), pages))

    st.markdown(f'<div class="smallcap">Total tokens: <b>{total:,}</b> • Page: <b>{page:,}/{pages:,}</b> • Items per page: <b>{PAGE_SIZE}</b></div>', unsafe_allow_html=True)

    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    page_df = f.iloc[start:end].copy()

    missing = page_df[page_df["image_url"].astype(str).str.strip() == ""]["ca"].tolist()
    helius_map: Dict[str, Dict[str, Any]] = {}
    if missing:
        with st.spinner(f"Fetching missing images from Helius ({len(missing)})…"):
            helius_map = helius_get_asset_batch(missing)

    still_missing = []
    for m in missing:
        img = normalize_url(nonempty_str((helius_map.get(m) or {}).get("image")))
        if not img:
            still_missing.append(m)

    metaplex_map: Dict[str, Dict[str, Any]] = {}
    if still_missing:
        with st.spinner(f"Trying Metaplex on-chain metadata ({len(still_missing)})…"):
            metaplex_map = metaplex_resolve_images(still_missing)

    items = page_df.to_dict(orient="records")

    for r in range(0, len(items), COLS):
        cols = st.columns(COLS)
        for j in range(COLS):
            idx = r + j
            if idx >= len(items):
                continue
            it = items[idx]

            ca = str(it.get("ca", "")).strip()
            sym = str(it.get("symbol", "")).strip()
            nm = str(it.get("name", "")).strip()
            mint_time = str(it.get("mint_time", "")).strip()

            img = normalize_url(nonempty_str(it.get("image_url", "")))

            reason = "Image failed to load"
            if not img:
                img = normalize_url(nonempty_str((helius_map.get(ca) or {}).get("image")))
                reason = (helius_map.get(ca) or {}).get("reason") or reason

                if not img:
                    img = normalize_url(nonempty_str((metaplex_map.get(ca) or {}).get("image")))
                    reason = (metaplex_map.get(ca) or {}).get("reason") or reason

                if not img:
                    reason = "No image URL in list (Helius + Metaplex failed)"
            else:
                if is_local_sent(img):
                    reason = "Image replaced locally"

            key = f"{page}_{idx}"

            card_html = textwrap.dedent(f"""
    <div class="token-card">
      <div class="token-top">
        <div>
          <div class="token-sym">{html.escape(sym or "—")}</div>
          <div class="token-name">{html.escape(nm or "")}</div>
        </div>
      </div>
      {image_box_html(img, reason, key, local_map).strip()}
      <div class="token-meta">
        <div class="token-ca">{html.escape(short_ca(ca))}</div>
        <div class="token-date">{html.escape(mint_time)}</div>
      </div>
    </div>
""").strip()
            with cols[j]:
                st.markdown(card_html, unsafe_allow_html=True)
                components.html(copy_button_html(ca, key=f"{key}_{j}"), height=44)
                st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

# ============================
# STATISTICS TAB
# ============================
with stats_tab:
    years = sorted([int(y) for y in df["mint_dt"].dropna().dt.year.unique().tolist()])
    if not years:
        st.info("No mint dates available to build statistics.")
    else:
        default_year = years[-1]
        sel_year = st.selectbox("Year", options=years, index=years.index(default_year))

        ydf = df[df["mint_dt"].dt.year == int(sel_year)].copy()
        ydf = ydf.dropna(subset=["mint_dt"])

        daily = ydf["mint_dt"].dt.date.value_counts()
        daily = daily.sort_index()

        start_day = pd.Timestamp(dt.date(int(sel_year), 1, 1))
        end_day = pd.Timestamp(dt.date(int(sel_year), 12, 31))
        all_days = pd.date_range(start_day, end_day, freq="D").date

        daily = daily.reindex(all_days, fill_value=0)

        chart_df = pd.DataFrame({"date": list(all_days), "tokens": daily.values}).set_index("date")

        st.markdown(f'<div class="smallcap">Tokens minted per day in <b>{int(sel_year)}</b></div>', unsafe_allow_html=True)
        st.line_chart(chart_df)

        top10 = daily.sort_values(ascending=False).head(10)
        st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
        st.markdown('<div class="smallcap"><b>Top 10 days</b> (click to open the Archive filtered to that day)</div>', unsafe_allow_html=True)

        for d, cnt in top10.items():
            ds = d.isoformat()
            row_html = f'''
<div class="token-card" style="padding:12px 12px;">
  <div class="token-meta" style="margin-top:0;">
    <div class="token-ca" style="font-weight:700;">{html.escape(ds)}</div>
    <div class="token-date" style="font-weight:700;">{int(cnt)} tokens</div>
  </div>
  <div style="margin-top:10px;">
    <a class="copy-btn" style="display:inline-block; text-decoration:none; text-align:center;" href="?date={html.escape(ds)}" target="_self">Open this day</a>
  </div>
</div>
'''
            st.markdown(row_html, unsafe_allow_html=True)

with chain_tab:
    st.markdown('<div class="yzy-title"><h1>YZY BLOCKCHAIN</h1><h2>COMING SOON</h2></div>', unsafe_allow_html=True)
    try:
        st.image('soon.png', use_container_width=True)
    except Exception:
        st.info('soon.png not found ')
