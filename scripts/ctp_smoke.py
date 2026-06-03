"""Headless CTP connectivity smoke test (no GUI, no orders).

Logs into SimNow via the configured connect_ctp.json, waits for the contract
download, auto-subscribes to liquid rb*.SHFE contracts, and prints live ticks
for ~20s. Sends ZERO orders. A pre-flight diagnostic — see docs/simnow_preflight.md.

Run from anywhere: `python scripts/ctp_smoke.py`. Optional env overrides:
CTP_GATEWAY=ctptest, CTP_TD=tcp://host:port, CTP_MD=tcp://host:port.
"""

# vnpy pins TEMP_DIR to <cwd>/.vntrader at import time, so we must os.chdir into
# the workspace BEFORE importing vnpy — hence imports are intentionally not all
# at the top of the file.
# ruff: noqa: E402

import json
import os
import sys
import time
from pathlib import Path

WS = Path(r"C:\Quant\vnpy_workspace")
os.chdir(WS)  # match LIVE: vnpy pins TEMP_DIR to <cwd>/.vntrader at import
sys.path.insert(0, str(WS.parent))
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]  # GBK console: never crash on a glyph
except Exception:
    pass

from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_TICK
from vnpy.trader.object import SubscribeRequest

if os.environ.get("CTP_GATEWAY", "ctp").lower() == "ctptest":
    from vnpy_ctptest import CtptestGateway as Gateway  # SimNow 看穿式 (6.7.2)
else:
    from vnpy_ctp import CtpGateway as Gateway  # production (6.7.11)
print(f">>> gateway: {Gateway.__name__} (default_name={Gateway.default_name})")

setting = json.loads((WS / ".vntrader" / "connect_ctp.json").read_text(encoding="utf-8"))

# Optional in-memory server overrides (creds untouched) for testing alt fronts.
if os.environ.get("CTP_TD"):
    setting["交易服务器"] = os.environ["CTP_TD"]
if os.environ.get("CTP_MD"):
    setting["行情服务器"] = os.environ["CTP_MD"]
print(f">>> td={setting['交易服务器']}  md={setting['行情服务器']}")

contracts: dict = {}
tick_count: dict = {}
last_price: dict = {}


def on_log(event):
    print(f"[LOG] {event.data.msg}")


def on_contract(event):
    c = event.data
    contracts[c.vt_symbol] = c


def on_tick(event):
    t = event.data
    tick_count[t.vt_symbol] = tick_count.get(t.vt_symbol, 0) + 1
    last_price[t.vt_symbol] = t.last_price


ee = EventEngine()
me = MainEngine(ee)
me.add_gateway(Gateway)
ee.register(EVENT_LOG, on_log)
ee.register(EVENT_CONTRACT, on_contract)
ee.register(EVENT_TICK, on_tick)

print(">>> Connecting to SimNow CTP (NO orders will be sent) ...")
me.connect(setting, Gateway.default_name)

# Wait for the instrument query to finish (contract count stabilizes).
deadline = time.time() + 25
prev = -1
while time.time() < deadline:
    time.sleep(2)
    n = len(contracts)
    print(f"    contracts downloaded: {n}")
    if n > 0 and n == prev:
        break
    prev = n

print(f"\n>>> Total contracts: {len(contracts)}")
if not contracts:
    print(">>> No contracts -> login/auth FAILED. See [LOG] lines above for the CTP reason.")
    me.close()
    time.sleep(1)
    sys.exit(2)

# Auto-pick liquid contracts: all rb*.SHFE (rebar) near/main months.
rb = sorted(
    c
    for c in contracts.values()
    if c.exchange == Exchange.SHFE and c.symbol.lower().startswith("rb") and c.symbol[2:].isdigit()
)
rb = sorted(rb, key=lambda c: c.symbol)
targets = rb if rb else list(contracts.values())[:8]
print(f">>> rb.SHFE contracts found: {[c.symbol for c in rb]}")
print(f">>> Subscribing to {len(targets)} contracts and collecting ticks for 20s ...")
for c in targets:
    me.subscribe(SubscribeRequest(symbol=c.symbol, exchange=c.exchange), Gateway.default_name)

time.sleep(20)

print("\n--- TICK SUMMARY ---")
if tick_count:
    for vs, n in sorted(tick_count.items(), key=lambda x: -x[1]):
        print(f"    {vs}: {n} ticks  last={last_price.get(vs)}")
    main = max(tick_count, key=lambda vs: tick_count[vs])
    print(f"\n>>> Most active (likely current main contract): {main}")
else:
    print("    No ticks received (market quiet / outside session for these symbols).")
    print("    Login + contract download still PROVES connectivity.")

me.close()
time.sleep(1)
print(">>> Closed cleanly.")
