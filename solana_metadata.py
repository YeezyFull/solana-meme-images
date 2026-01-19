from __future__ import annotations

import os
import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple

import httpx
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from concurrent.futures import ThreadPoolExecutor, as_completed

# Metaplex Token Metadata program
METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")


# ---------------------------
# Optional Helius DAS fallback
# ---------------------------
HELIUS_DAS_ENDPOINT = "https://mainnet.helius-rpc.com/"
HELIUS_API_KEY_INLINE = "2314091d-cb57-454c-8ae0-85d261c91824"  # paste your key here (fallback for No Data)
HELIUS_API_KEY_ENV = "HELIUS_API_KEY"
HELIUS_BATCH_SIZE = 100


def _get_helius_url() -> Optional[str]:
    key = (HELIUS_API_KEY_INLINE or "").strip() or (os.getenv(HELIUS_API_KEY_ENV) or "").strip()
    if not key:
        return None
    return f"{HELIUS_DAS_ENDPOINT}?api-key={key}"


def _safe_str(x) -> Optional[str]:
    if isinstance(x, str):
        s = x.strip()
        if not s or s in ("0",) or s.lower() in ("null", "none"):
            return None
        return s
    return None


def fetch_helius_assets_batch(mints: list[str], timeout_s: float = 12.0) -> dict[str, dict[str, Optional[str]]]:
    """
    Helius DAS getAssetBatch.
    Returns mint -> {image, uri, name, symbol}
    """
    url = _get_helius_url()
    if not url or not mints:
        return {}

    out: dict[str, dict[str, Optional[str]]] = {}
    client = httpx.Client(timeout=timeout_s, follow_redirects=True, headers=_HTTP_HEADERS)

    for i in range(0, len(mints), HELIUS_BATCH_SIZE):
        ids = mints[i:i+HELIUS_BATCH_SIZE]
        payload = {"jsonrpc": "2.0", "id": "1", "method": "getAssetBatch", "params": {"ids": ids}}
        try:
            r = client.post(url, json=payload, headers={"Content-Type": "application/json"})
            if r.status_code in (408, 429, 500, 502, 503, 504):
                time.sleep(0.35)
                r = client.post(url, json=payload, headers={"Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
            assets = data.get("result")
            if not isinstance(assets, list):
                continue

            for a in assets:
                if not isinstance(a, dict):
                    continue
                mint = a.get("id")
                if not isinstance(mint, str):
                    continue

                content = a.get("content") or {}
                meta = content.get("metadata") or {}

                # prefer CDN image
                img = None
                files = content.get("files")
                if isinstance(files, list) and files:
                    f0 = files[0] if isinstance(files[0], dict) else None
                    if isinstance(f0, dict):
                        img = f0.get("cdn_uri") or f0.get("uri")
                if not img and isinstance(content.get("links"), dict):
                    img = content["links"].get("image")

                uri = content.get("json_uri")
                name = meta.get("name")
                symbol = meta.get("symbol")
                ti = a.get("token_info") or {}
                if not symbol and isinstance(ti, dict):
                    symbol = ti.get("symbol")

                out[mint] = {
                    "image": _safe_str(img),
                    "uri": _safe_str(uri),
                    "name": _safe_str(name),
                    "symbol": _safe_str(symbol),
                }
        except Exception:
            continue

    return out
# A few IPFS gateways for reliability when URIs are on IPFS
DEFAULT_IPFS_GATEWAYS: list[str] = [
    "https://cloudflare-ipfs.com",
    "https://nftstorage.link",
    "https://w3s.link",
    "https://dweb.link",
    "https://ipfs.io",
    "https://gateway.pinata.cloud",
]

_HTTP_HEADERS = {
    "User-Agent": "token-image-scraper/1.0",
    "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
}


@dataclass
class TokenView:
    mint: str
    name: Optional[str] = None
    symbol: Optional[str] = None
    image: Optional[str] = None
    uri: Optional[str] = None
    status: str = "unknown"  # ok | no_metadata | no_uri | http_error | parse_error
    error: Optional[str] = None
    last_url: Optional[str] = None
    http_status: Optional[int] = None


def get_metadata_pda(mint: Pubkey) -> Pubkey:
    seeds = [b"metadata", bytes(METADATA_PROGRAM_ID), bytes(mint)]
    pda, _bump = Pubkey.find_program_address(seeds, METADATA_PROGRAM_ID)
    return pda


def _parse_uri_from_metaplex_raw(data: bytes) -> Optional[str]:
    """
    Very simple parsing (like your snippet):
    scan raw bytes for http(s):// and return until first null byte.
    """
    if not data:
        return None
    # Search in bytes for http(s) scheme
    m = re.search(rb"https?://[^\x00\s\"']+", data)
    if not m:
        return None
    uri = m.group(0).decode("utf-8", errors="ignore").strip()
    # Strip trailing odd chars
    uri = uri.rstrip("\x00").strip()
    return uri or None


def _swap_ipfs_gateway(url: str, gateways: list[str]) -> list[str]:
    """
    If url contains /ipfs/<cid...>, try other gateways with same path.
    """
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return [u]
    m = re.match(r"^(https?://[^/]+)(/ipfs/.*)$", u)
    if not m:
        return [u]
    path = m.group(2)
    out = [g.rstrip("/") + path for g in gateways]
    out.append(u)
    # de-dup
    seen = set()
    res = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        res.append(x)
    return res


def _fetch_json(uri: str, gateways: list[str], timeout_s: float = 10.0) -> Dict[str, Any]:
    client = httpx.Client(timeout=timeout_s, follow_redirects=True, headers=_HTTP_HEADERS)
    last_err: Optional[Exception] = None
    for url in _swap_ipfs_gateway(uri, gateways):
        try:
            r = client.get(url)
            # Retry once on common transient errors
            if r.status_code in (408, 429, 500, 502, 503, 504):
                time.sleep(0.25)
                r = client.get(url)
            r.raise_for_status()

            ctype = (r.headers.get("content-type") or "").lower()
            # Some gateways return HTML error pages with 200; detect that
            text = r.text
            if "text/html" in ctype or text.lstrip().startswith("<!doctype html") or "<html" in text[:200].lower():
                raise ValueError("Non-JSON HTML response")
            return r.json()
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Failed to fetch JSON from uri: {uri} ({last_err})")


def _extract_image_url(j: Dict[str, Any]) -> Optional[str]:
    if not isinstance(j, dict):
        return None
    # Primary
    v = j.get("image")
    if isinstance(v, str) and v.strip():
        return v.strip()
    # Common fallbacks
    for k in ("image_url", "imageUrl", "logoURI", "logoUri", "animation_url", "animationUrl"):
        v = j.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Nested extensions/properties
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
                    u = f.get("uri") or f.get("url")
                    if isinstance(u, str) and u.strip():
                        return u.strip()
    return None


def _resolve_one(tv: TokenView, uri: str, gateways: list[str]) -> TokenView:
    tv.uri = uri
    try:
        j = _fetch_json(uri, gateways=gateways)
        img = _extract_image_url(j)
        if img:
            tv.image = img
            tv.status = "ok"
        else:
            tv.status = "parse_error"
            tv.error = "JSON has no image field"
        return tv
    except Exception as e:
        tv.status = "http_error"
        tv.error = str(e)
        return tv


def get_token_views_batch(
    client: Client,
    mints: list[str],
    gateways: Optional[list[str]] = None,
    max_workers: int = 16,
) -> list[TokenView]:
    """
    Primary (as requested):
      Metaplex metadata PDA -> token URI (simple scan) -> fetch JSON -> image URL

    Fallback:
      If Metaplex account is missing ("No Data"/no_metadata), try Helius DAS for those mints
      (only if you set HELIUS_API_KEY_INLINE or env HELIUS_API_KEY).
    """
    gateways = gateways or DEFAULT_IPFS_GATEWAYS
    results: list[TokenView] = [TokenView(mint=m) for m in mints]

    # 1) Batch fetch metaplex metadata accounts
    pdas: list[Pubkey] = []
    idx: list[int] = []
    for i, m in enumerate(mints):
        try:
            mint_pk = Pubkey.from_string(m)
            pdas.append(get_metadata_pda(mint_pk))
            idx.append(i)
        except Exception as e:
            results[i].status = "parse_error"
            results[i].error = f"Invalid mint: {e}"

    uri_tasks: list[Tuple[TokenView, str]] = []

    if pdas:
        resp = client.get_multiple_accounts(pdas, encoding="base64")
        if hasattr(resp, "to_json"):
            resp = json.loads(resp.to_json())

        values = (resp or {}).get("result", {}).get("value", []) or []

        for local_i, acc in enumerate(values):
            tv = results[idx[local_i]]
            if not acc:
                tv.status = "no_metadata"
                tv.error = "No metaplex metadata account"
                continue

            data_field = acc.get("data")
            data_b64 = None
            if isinstance(data_field, (list, tuple)) and data_field and isinstance(data_field[0], str):
                data_b64 = data_field[0]
            elif isinstance(data_field, str):
                data_b64 = data_field

            if not data_b64:
                tv.status = "no_metadata"
                tv.error = "Empty metaplex account data"
                continue

            try:
                raw = base64.b64decode(data_b64)
                uri = _parse_uri_from_metaplex_raw(raw)
                if not uri:
                    tv.status = "no_uri"
                    tv.error = "No URI found in metaplex data"
                    continue
                uri_tasks.append((tv, uri))
            except Exception as e:
                tv.status = "parse_error"
                tv.error = str(e)

    # 2) Fetch JSON in parallel and extract image URLs
    if uri_tasks:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_resolve_one, tv, uri, gateways): tv for tv, uri in uri_tasks}
            for fut in as_completed(futs):
                _ = fut.result()

    # 3) Helius fallback for tokens with No Data (no_metadata)
    need_helius = [tv.mint for tv in results if tv.status == "no_metadata" and not tv.image]
    if need_helius:
        assets = fetch_helius_assets_batch(need_helius)
        if assets:
            for tv in results:
                if tv.mint not in assets:
                    continue
                hit = assets[tv.mint]
                # If helius gives image, use it
                img = hit.get("image")
                uri = hit.get("uri")
                if hit.get("name") and not tv.name:
                    tv.name = hit.get("name")
                if hit.get("symbol") and not tv.symbol:
                    tv.symbol = hit.get("symbol")

                if img:
                    tv.image = img
                    tv.status = "ok"
                    tv.error = None
                elif uri:
                    # some helius assets provide usable direct image URL in uri
                    tv.image = uri
                    tv.status = "ok"
                    tv.error = None
                else:
                    tv.error = (tv.error or "") + " | Helius had no image/uri"
    return results
