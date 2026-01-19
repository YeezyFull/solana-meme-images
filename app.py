import csv
import math
import os
import time
from dataclasses import asdict

import streamlit as st
import streamlit.components.v1 as components
import html

from solana.rpc.api import Client
from solana_metadata import get_token_views_batch, TokenView


# -----------------------------
# UI helpers
# -----------------------------

def copy_button(text: str, key: str):
    """HTML/JS clipboard copy button (works on older Streamlit)."""
    safe = html.escape(text)
    bid = f"copy_{key}".replace(" ", "_").replace(":", "_").replace("/", "_")
    components.html(
        f"""
        <div style="margin-top:2px;margin-bottom:6px;display:flex;gap:6px;align-items:center;">
          <button id="{bid}" style="
            font-size:11px;padding:4px 8px;border-radius:10px;
            border:1px solid rgba(0,0,0,.15);background:white;cursor:pointer;">
            Copy CA
          </button>
          <span id="{bid}_msg" style="font-size:11px;color:rgba(0,0,0,.55);"></span>
        </div>
        <script>
          const btn = document.getElementById("{bid}");
          const msg = document.getElementById("{bid}_msg");
          if (btn) {{
            btn.onclick = async () => {{
              try {{
                await navigator.clipboard.writeText("{safe}");
                msg.textContent = "Copied!";
                setTimeout(() => msg.textContent = "", 900);
              }} catch (e) {{
                msg.textContent = "Copy failed";
                setTimeout(() => msg.textContent = "", 1200);
              }}
            }};
          }}
        </script>
        """,
        height=44,
    )


def st_image_compat(url: str):
    """Streamlit API changed from use_column_width -> use_container_width."""
    try:
        return st.image(url, use_container_width=True)
    except TypeError:
        try:
            return st.image(url, use_column_width=True)
        except TypeError:
            return st.image(url)


def safe_str(x, fallback="—"):
    if x is None:
        return fallback
    if isinstance(x, str):
        s = x.strip()
        if not s or s == "0" or s.lower() in ("null", "none"):
            return fallback
        return s
    return fallback


def load_mints_from_csv(path: str) -> list[str]:
    mints: list[str] = []
    if not os.path.exists(path):
        return mints

    with open(path, "r", newline="", encoding="utf-8") as f:
        # Accept either: single-column CSV, or any CSV containing something that looks like base58 mint
        reader = csv.reader(f)
        for row in reader:
            for cell in row:
                c = (cell or "").strip()
                if c and len(c) >= 32:
                    mints.append(c)
                    break
    # de-dup preserve order
    seen = set()
    out = []
    for m in mints:
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


# -----------------------------
# App
# -----------------------------

st.set_page_config(page_title="Token Images", layout="wide")

# Minimal CSS for dense grid
st.markdown(
    """
    <style>
      .block-container { padding-top: 10px; padding-bottom: 10px; }
      [data-testid="stCaptionContainer"] p { margin-bottom: 0.15rem; }
      .tiny { font-size: 11px; color: rgba(0,0,0,.65); }
      .mint { font-size: 11px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
      .tile { border: 1px dashed rgba(0,0,0,.18); border-radius: 14px; padding: 6px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Token images (Metaplex → URI → JSON → image)")

show_debug = st.checkbox("Show debug details (status / source / URL / error)", value=False)

csv_path = os.path.join(os.path.dirname(__file__), "full.csv")
mints_all = load_mints_from_csv(csv_path)

if not mints_all:
    st.error("full.csv not found or empty. Put full.csv next to app.py.")
    st.stop()

PAGE_SIZE = 100
cols_per_row = 10

total = len(mints_all)
pages = max(1, math.ceil(total / PAGE_SIZE))

c1, c2, c3, c4 = st.columns([1.1, 1, 1, 2.5])
with c1:
    page = st.number_input("Page", min_value=1, max_value=pages, value=1, step=1)
with c2:
    rpc_url = st.text_input("RPC", value="https://api.mainnet-beta.solana.com")
with c3:
    st.write(f"Total: **{total:,}**")
    st.write(f"Pages: **{pages:,}**")
with c4:
    st.write("")

start = (page - 1) * PAGE_SIZE
end = min(total, start + PAGE_SIZE)
page_mints = mints_all[start:end]

client = Client(rpc_url)

progress = st.progress(0)
status_placeholder = st.empty()

# Fetch in chunks to keep UI responsive
CHUNK = 50
results: list[TokenView] = []
t0 = time.time()

for i in range(0, len(page_mints), CHUNK):
    chunk = page_mints[i:i+CHUNK]
    status_placeholder.caption(f"Fetching {i+1}-{min(i+len(chunk), len(page_mints))} / {len(page_mints)} …")
    try:
        tvs = get_token_views_batch(client, chunk)
    except Exception as e:
        tvs = [TokenView(mint=m, status="error", error=str(e)) for m in chunk]
    results.extend(tvs)
    progress.progress(min(1.0, (i + len(chunk)) / max(1, len(page_mints))))

dt = time.time() - t0
status_placeholder.caption(f"Done in {dt:.1f}s. Images: {sum(1 for r in results if r.image)} / {len(results)}")
progress.empty()

# Breakdown
status_counts = {}
http_counts = {}
source_counts = {}
for r in results:
    status_counts[r.status] = status_counts.get(r.status, 0) + 1
    src = getattr(r, "source", "none")
    source_counts[src] = source_counts.get(src, 0) + 1
    if getattr(r, "http_status", None):
        code = int(r.http_status)
        http_counts[code] = http_counts.get(code, 0) + 1

st.caption("Status breakdown: " + ", ".join([f"**{k}**: {v}" for k, v in sorted(status_counts.items(), key=lambda x: (-x[1], x[0]))]))
st.caption("Source breakdown: " + ", ".join([f"**{k}**: {v}" for k, v in sorted(source_counts.items(), key=lambda x: (-x[1], x[0]))]))
if http_counts:
    st.caption("HTTP status breakdown: " + ", ".join([f"**{k}**: {v}" for k, v in sorted(http_counts.items())]))

st.divider()

# Render 10x10 grid
rows = math.ceil(len(results) / cols_per_row)

idx = 0
for r in range(rows):
    cols = st.columns(cols_per_row, gap="small")
    for c in range(cols_per_row):
        if idx >= len(results):
            break
        tv = results[idx]
        idx += 1

        with cols[c]:
            # Tile container
            st.markdown('<div class="tile">', unsafe_allow_html=True)

            img_url = tv.image if isinstance(tv.image, str) else None
            render_err = None
            if img_url and img_url.strip() and img_url.strip() != "0":
                try:
                    st_image_compat(img_url)
                except Exception as e:
                    render_err = str(e)
                    # If rendering fails, show placeholder
                    st.markdown('<div style="height:120px;"></div>', unsafe_allow_html=True)
            else:
                st.markdown('<div style="height:120px;"></div>', unsafe_allow_html=True)

            sym = safe_str(tv.symbol, fallback="—")
            st.markdown(f"**{sym}**", help=safe_str(tv.name, fallback=""))

            mint = tv.mint
            short = mint[:4] + "…" + mint[-4:] if isinstance(mint, str) and len(mint) > 12 else mint
            st.markdown(f'<div class="mint" title="{html.escape(mint)}">{html.escape(short)}</div>', unsafe_allow_html=True)

            copy_button(mint, key=f"{page}_{r}_{c}_{idx}")

            if show_debug and (tv.status != "ok" or render_err is not None):
                st.markdown(
                    f'<div class="tiny">'
                    f'status={html.escape(str(tv.status))} '
                    f'source={html.escape(str(getattr(tv,"source","?")))} '
                    f'code={html.escape(str(getattr(tv,"http_status", "")))}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                if render_err:
                    st.markdown(f'<div class="tiny">render_err: {html.escape(render_err[:140])}</div>', unsafe_allow_html=True)
                if getattr(tv, "uri", None):
                    u = str(tv.uri)
                    st.markdown(f'<div class="tiny">uri: {html.escape(u[:120])}{"…" if len(u)>120 else ""}</div>', unsafe_allow_html=True)
                if getattr(tv, "last_url", None):
                    u = str(tv.last_url)
                    st.markdown(f'<div class="tiny">url: {html.escape(u[:120])}{"…" if len(u)>120 else ""}</div>', unsafe_allow_html=True)
                if getattr(tv, "error", None):
                    e = str(tv.error)
                    st.markdown(f'<div class="tiny">err: {html.escape(e[:140])}{"…" if len(e)>140 else ""}</div>', unsafe_allow_html=True)

            st.markdown("</div>", unsafe_allow_html=True)
