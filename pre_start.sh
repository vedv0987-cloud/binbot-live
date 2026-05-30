#!/bin/bash
# v14.6.5 AUDIT FIX: removed the regex block that silently overwrote MAX_POSITIONS,
# RISK_PCT, and MAX_EXPOSURE in config.py on every boot (was hardcoded SNIPER_90
# tier: max_pos=1, risk=0.02, max_exp=0.90). config.py is now the single source
# of truth for these values. DD auto-fix and orphan reconcile blocks preserved.

/usr/bin/python3 << 'PY'
import os, json
try:
    with open('/home/ubuntu/binbot_live/.env') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k,v = line.strip().split('=',1)
                os.environ[k] = v.strip('"').strip("'")
    from binance.client import Client
    c = Client(os.environ['BINANCE_API_KEY'], os.environ['BINANCE_API_SECRET'])

    # Use TOTAL portfolio value (free USDT + open positions) for DD baseline
    free_usdt = float(c.get_asset_balance(asset='USDT')['free'])
    p = '/home/ubuntu/binbot_live/bot_state.json'
    s = json.load(open(p))
    pos_val = sum(px.get('size', 0) for px in s.get('positions', []))
    total = free_usdt + pos_val   # real portfolio value

    # v14.6.5: config rewrite removed. config.py owns MAX_POSITIONS / RISK_PCT / MAX_EXPOSURE.
    # Display only: read current values from config.py so the boot banner is honest.
    try:
        import sys
        sys.path.insert(0, '/home/ubuntu/binbot_live')
        from config import Config
        _cfg_disp = Config()
        max_pos_disp = _cfg_disp.MAX_POSITIONS
        risk_disp    = _cfg_disp.RISK_PCT
        max_exp_disp = _cfg_disp.MAX_EXPOSURE
    except Exception as _e:
        max_pos_disp, risk_disp, max_exp_disp = "?", 0.0, 0.0
        print(f"⚠️ Pre-start could not read config.py: {_e}")

    # DD fix — uses total equity
    peak = s.get('peak_equity', 0)
    if peak > total * 1.10:
        s['peak_equity'] = total
        s['dd_peak'] = total
        json.dump(s, open(p, 'w'), indent=2)
        print(f"✅ DD auto-fixed: ${peak:.2f} → ${total:.2f}")
    else:
        print(f"✅ DD OK: peak=${peak:.2f} equity=${total:.2f}")

    print(f"✅ Config (from config.py) | free=${free_usdt:.2f} pos=${pos_val:.2f} total=${total:.2f} | "
          f"max_pos={max_pos_disp} | risk={risk_disp*100:.2f}% | max_exp={max_exp_disp*100:.0f}%")
except Exception as e:
    print(f"Pre-start skipped: {e}")
PY

# ═══ AUTO-RECONCILE: fix ghost + orphan positions on every boot ═══
python3 << 'PY'
import os, json, sys, math
from datetime import datetime, timezone
try:
    with open('/home/ubuntu/binbot_live/.env') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k,v=line.strip().split('=',1); os.environ[k]=v.strip('"').strip("'")
    from binance.client import Client
    c = Client(os.environ['BINANCE_API_KEY'], os.environ['BINANCE_API_SECRET'])
    s = json.load(open('/home/ubuntu/binbot_live/bot_state.json'))

    changed = False

    # 1. Remove GHOST positions (bot tracks but wallet has 0)
    clean = []
    for p in s.get('positions', []):
        asset = p['pair'].replace('USDT','')
        try:
            bal = c.get_asset_balance(asset=asset)
            total = float(bal['free']) + float(bal['locked'])
        except: total = 0
        if total < 0.001:
            print(f"🧹 GHOST removed: {p['pair']} (bot qty={p['qty']}, wallet=0)")
            changed = True
        else:
            clean.append(p)
    s['positions'] = clean

    # 2. Add ORPHAN positions (wallet has coins + SL order, bot doesn't track)
    tracked_pairs = {p['pair'] for p in s['positions']}
    account = c.get_account()
    for asset in account['balances']:
        sym = asset['asset'] + 'USDT'
        if sym in tracked_pairs or asset['asset'] == 'USDT': continue
        total = float(asset['free']) + float(asset['locked'])
        if total < 0.001: continue
        try:
            price = float(c.get_symbol_ticker(symbol=sym)['price'])
            val = total * price
            if val < 2.0: continue  # skip dust
            # Check for SL order on Binance
            orders = c.get_open_orders(symbol=sym)
            sl = next((o for o in orders if o['type'] in ('STOP_LOSS_LIMIT','STOP_LOSS')), None)
            if sl:
                sl_price = float(sl['stopPrice'])
                entry    = round(sl_price / 0.97, 6)
                tp       = round(entry * 1.045, 6)
                size = round(entry * total, 8)
                s['positions'].append({
                    "pair": sym, "entry": entry, "qty": total, "size": size,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "sl": sl_price, "tp": tp, "group": "C",
                    "high": entry, "strategy": "ORPHAN_RECOVERED", "atr": 0.0,
                    "trailing_on": False, "trail_stop": 0.0,
                    "safety_used": 0, "avg_entry": entry,
                    "total_qty": total, "total_cost": size,
                    "scale_done": [], "rr": 1.5, "grade": "A+",
                    "entry_fee": size * 0.001, "context": "PRE_START_RECOVERY",
                    "be_locked": False, "pyramids": 0, "sell_fails": 0,
                    "native_sl_order_id": int(sl['orderId'])
                })
                print(f"✅ ORPHAN added: {sym} qty={total:.4f} entry~${entry:.5f} SL=${sl_price}")
                changed = True
            else:
                if val > 5.0: print(f"⚠️  {sym}: {total:.4f} coins (${val:.2f}) — no SL order, needs manual review")
        except Exception as e:
            pass

    if changed:
        json.dump(s, open('/home/ubuntu/binbot_live/bot_state.json','w'), indent=2)
        print(f"✅ State reconciled: {len(s['positions'])} positions tracked")
    else:
        print(f"✅ Reconcile: clean — {len(s['positions'])} positions")
except Exception as e:
    print(f"⚠️  Reconcile skipped: {e}")
PY
