# BinBot v11 — tests/test_core.py
# 10 core unit tests covering critical risk/execution paths
import sys, os, unittest, asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# Add parent dir to path so we can import bot modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Position, Signal, Context
from analytics import DrawdownShield


class MockConfig:
    """Minimal config for testing — mirrors real config defaults."""
    TOTAL_CAPITAL = 50.0
    MAX_POSITIONS = 5
    MAX_EXPOSURE = 0.85
    MIN_TRADE = 5.0
    MAX_DAILY_TRADES = 20
    MAX_CONSEC_LOSSES = 5
    LOSS_PAUSE_MIN = 120
    MAX_HOLD_MIN = 720
    CIRCUIT_BREAKER_PCT = 0.10
    MAX_HEAT = 0.15
    FEAR_HEAT = 0.08
    KELLY_FRACTION = 0.25
    USE_KELLY = False
    TAKER_FEE = 0.001
    MAKER_FEE = 0.001
    USE_LIMIT = False
    MAX_SLIP_PCT = 0.005
    risk_amount = 1.0
    max_daily_loss = 5.0
    CORR_THRESHOLD = 0.95
    SL_TRIGGER_BUFFER = 0.003
    SCALE_OUT = False
    PYRAMID_ENABLED = False
    LOG_FILE = "test_trades.jsonl"
    # v18.8.9 Scale Ladder
    PROFIT_LADDER_ENABLED = True
    PROFIT_LADDER_LEVELS = ((0.015, 0.000), (0.025, 0.010), (0.035, 0.020))
    PROFIT_LADDER_SCALE_PCT = 0.30
    PROFIT_LADDER_MIN_SLICE_USD = 5.0
    PARTIAL_SCALEOUT_ENABLED = True
    PARTIAL_SCALEOUT_PCT = 0.40
    USE_MULTI_TIER_LADDER = True
    # v18.9.0 Session filter — OFF in tests so the other can_trade tests are unaffected;
    # the logic is exercised directly via _session_ok in TestSessionFilter.
    SESSION_FILTER_ENABLED = False
    SESSION_DST_AUTOSHIFT = True
    SESSION_GOLDEN = (1110, 1350)
    SESSION_WINDOWS = (
        ("ASIAN", 330, 810, frozenset({"XRP", "ADA"}), False),
        ("GOLDEN", 1110, 1350, frozenset({"BTC", "ETH"}), True),
        ("MEME", 1350, 210, frozenset({"DOGE", "ENJ"}), True),
    )


class MockStateManager:
    """State manager that stores in memory instead of disk."""
    def __init__(self):
        self.saved = None

    def load(self):
        return self.saved

    def save(self, *args, **kwargs):
        self.saved = {"called": True}


def make_signal(pair="BTCUSDT", price=100.0, sl=98.0, tp=104.0, group="A",
                conf=0.8, grade="A", strategy="SMC"):
    return Signal(pair=pair, price=price, strategy=strategy, conf=conf,
                  grade=grade, reason="test", group=group, tier=1,
                  tp=tp, sl=sl, rr=2.0, atr=1.0)


def make_position(pair="BTCUSDT", entry=100.0, qty=0.5, size=50.0, sl=98.0,
                  tp=104.0, group="A", strategy="SMC", avg_entry=None,
                  total_qty=None, entry_fee=0.05, atr=1.0):
    return Position(
        pair=pair, entry=entry, qty=qty, size=size,
        entry_time=datetime.now(timezone.utc).isoformat(),
        sl=sl, tp=tp, group=group, high=entry, strategy=strategy,
        atr=atr, avg_entry=avg_entry or entry,
        total_qty=total_qty or qty, total_cost=size,
        entry_fee=entry_fee
    )


class MockMonitor:
    """Stub for monitors that Risk.__init__ expects."""
    def should_avoid(self, *a): return False, ""
    def risk_mult(self): return 1.0


# ═══════════════════════════════════════════════════════
# TEST 1-3: can_trade() blocking logic
# ═══════════════════════════════════════════════════════

class TestCanTrade(unittest.TestCase):
    def setUp(self):
        self.cfg = MockConfig()
        self.sm = MockStateManager()
        # Patch monitors before Risk import
        with patch('risk.KellySizer', return_value=MagicMock(trade_history=[])):
            with patch('risk.EventCalendar', return_value=MockMonitor()):
                with patch('risk.MVRVMonitor'), \
                     patch('risk.OpenInterestMonitor'), \
                     patch('risk.TokenUnlockMonitor', return_value=MockMonitor()), \
                     patch('risk.TVLMonitor'), \
                     patch('risk.WhaleWalletMonitor'), \
                     patch('risk.StablecoinFlow'):
                    from risk import Risk
                    self.risk = Risk(self.cfg, self.sm)

    def test_blocks_max_positions(self):
        """TEST 1: can_trade() blocks when MAX_POSITIONS reached."""
        for i in range(self.cfg.MAX_POSITIONS):
            self.risk.positions.append(make_position(pair=f"COIN{i}USDT"))
        sig = make_signal(pair="NEWUSDT")
        ok, reason, size = self.risk.can_trade(sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "MaxPos")

    def test_blocks_held_pair(self):
        """TEST 2: can_trade() blocks duplicate pair."""
        self.risk.positions.append(make_position(pair="BTCUSDT"))
        sig = make_signal(pair="BTCUSDT")
        ok, reason, size = self.risk.can_trade(sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "Held")

    def test_cooldown_after_loss(self):
        """TEST 3: can_trade() enforces 8h cooldown after loss on same pair."""
        sig = make_signal(pair="ETHUSDT")
        # Record a recent close as LOSS
        self.risk.last_close["ETHUSDT"] = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.risk.last_result["ETHUSDT"] = "LOSS"
        ok, reason, size = self.risk.can_trade(sig)
        self.assertFalse(ok)
        self.assertIn("CD", reason)


# ═══════════════════════════════════════════════════════
# TEST 4-5: DrawdownShield
# ═══════════════════════════════════════════════════════

class TestDrawdownShield(unittest.TestCase):
    def test_tiers(self):
        """TEST 4: DrawdownShield transitions through tiers once dollar floors are met."""
        dd = DrawdownShield(1000)
        dd.update(1000); self.assertEqual(dd.status, "FULL"); self.assertEqual(dd.risk_multiplier, 1.0)
        dd.update(970);  self.assertEqual(dd.status, "CAUTION"); self.assertEqual(dd.risk_multiplier, 0.75)
        dd.update(940);  self.assertEqual(dd.status, "DEFENSIVE"); self.assertEqual(dd.risk_multiplier, 0.50)
        dd.update(900);  self.assertEqual(dd.status, "SURVIVAL"); self.assertEqual(dd.risk_multiplier, 0.25)
        dd.update(870);  self.assertEqual(dd.status, "KILLED"); self.assertEqual(dd.risk_multiplier, 0.0)

    def test_peak_persistence(self):
        """TEST 5: DrawdownShield peak survives set_peak() (simulates restart)."""
        dd = DrawdownShield(500)
        dd.update_peak(800)  # v14.1: realized equity raises the high-water mark
        self.assertEqual(dd.peak, 800)

        # Simulate restart: new shield with initial capital
        dd2 = DrawdownShield(500)
        self.assertEqual(dd2.peak, 500)  # Default is wrong

        # Simulate set_peak from saved state
        with patch.object(DrawdownShield, "_compute_true_peak_from_journal", return_value=800):
            dd2.set_peak(800)
        self.assertEqual(dd2.peak, 800)  # Now correct

        dd2.update(720)  # 10% DD from 800
        self.assertEqual(dd2.status, "SURVIVAL")


# ═══════════════════════════════════════════════════════
# TEST 6: State save/load roundtrip
# ═══════════════════════════════════════════════════════

class TestStatePersistence(unittest.TestCase):
    def test_save_load_roundtrip(self):
        """TEST 6: Positions, PnL, daily stats survive save/load cycle."""
        import tempfile, json
        from state import StateManager

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, dir='.') as f:
            tmp_path = f.name

        try:
            sm = StateManager(tmp_path)
            positions = [make_position(pair="BTCUSDT"), make_position(pair="ETHUSDT")]
            sm.save(positions, pnl=12.5, daily_pnl=3.2, daily_t=7,
                    wins=15, losses=5, fees=0.8,
                    last_reset="2026-05-08", closs=2,
                    peak_equity=55.0, dd_peak=52.0)

            loaded = sm.load()
            self.assertIsNotNone(loaded)
            self.assertEqual(len(loaded["positions"]), 2)
            self.assertEqual(loaded["pnl"], 12.5)
            self.assertEqual(loaded["daily_pnl"], 3.2)
            self.assertEqual(loaded["daily_trades"], 7)
            self.assertEqual(loaded["wins"], 15)
            self.assertEqual(loaded["losses"], 5)
            self.assertEqual(loaded["last_reset"], "2026-05-08")
            self.assertEqual(loaded["closs"], 2)
            self.assertEqual(loaded["peak_equity"], 55.0)
            self.assertEqual(loaded["dd_peak"], 52.0)
        finally:
            os.unlink(tmp_path)
            # Clean up backup if created
            bak = tmp_path + ".bak"
            if os.path.exists(bak): os.unlink(bak)


# ═══════════════════════════════════════════════════════
# TEST 7-8: Exit logic
# ═══════════════════════════════════════════════════════

class TestExits(unittest.TestCase):
    def setUp(self):
        self.cfg = MockConfig()
        self.sm = MockStateManager()
        with patch('risk.KellySizer', return_value=MagicMock(trade_history=[])):
            with patch('risk.EventCalendar', return_value=MockMonitor()):
                with patch('risk.MVRVMonitor'), \
                     patch('risk.OpenInterestMonitor'), \
                     patch('risk.TokenUnlockMonitor', return_value=MockMonitor()), \
                     patch('risk.TVLMonitor'), \
                     patch('risk.WhaleWalletMonitor'), \
                     patch('risk.StablecoinFlow'):
                    from risk import Risk
                    self.risk = Risk(self.cfg, self.sm)

    def test_sl_trigger(self):
        """TEST 7: check_exits() triggers SL at correct price (with buffer)."""
        pos = make_position(pair="BTCUSDT", entry=100.0, sl=95.0, tp=110.0)
        self.risk.positions.append(pos)

        # Price above SL trigger (with 0.3% buffer) — no exit
        sl_trigger = pos.sl * (1 + self.cfg.SL_TRIGGER_BUFFER)  # 95 * 1.003 = 95.285
        tickers = {"BTCUSDT": sl_trigger + 0.01}
        ctx = MagicMock(regime="RANGE")
        ex = MagicMock()
        tg = MagicMock()
        exits = asyncio.run(self.risk.check_exits(tickers, ctx, ex, tg))  # v18.8.5: check_exits is async (v15.4)
        self.assertEqual(len(exits), 0)

        # Price below SL trigger — should trigger SL
        tickers = {"BTCUSDT": sl_trigger - 0.01}
        exits = asyncio.run(self.risk.check_exits(tickers, ctx, ex, tg))  # v18.8.5: check_exits is async (v15.4)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0][2], "SL")

    def test_tp_reached_enters_chase_mode_no_sell(self):
        """TEST 8 (rewritten v18.8.5): hitting TP no longer closes the position.
        Since the v14.7+ 'chase mode' redesign (risk.py:1084 — "TP REACHED, pin SL
        at TP, NO SELL"), the bot lets winners run and exits later via the ratcheting
        trail. Verify the CURRENT contract: price >= TP returns NO immediate exit,
        sets tp_floor_locked, and pins SL up to TP. The old test asserted the removed
        fixed-TP-close behavior and was one of the stale failures fixed in v18.8.5."""
        pos = make_position(pair="ETHUSDT", entry=100.0, sl=95.0, tp=110.0)
        self.risk.positions.append(pos)
        self.risk.tg = None  # skip Telegram sends inside the chase-mode path

        tickers = {"ETHUSDT": 111.0}  # Above TP
        ctx = MagicMock(regime="RANGE")
        ex = MagicMock()
        tg = MagicMock()
        exits = asyncio.run(self.risk.check_exits(tickers, ctx, ex, tg))
        # No immediate TP sell — chase mode lets the winner run.
        self.assertEqual(len(exits), 0,
            "TP reached should pin SL and chase, not sell immediately")
        # Profit locked: tp_floor_locked set and SL pinned up to (at least) TP.
        self.assertTrue(getattr(pos, "tp_floor_locked", False),
            "tp_floor_locked must be set once price reaches TP")
        self.assertGreaterEqual(pos.sl, 110.0 - 1e-6,
            "SL must be pinned up to TP after TP reached (chase floor)")


# ═══════════════════════════════════════════════════════
# TEST 9: DCA weighted average
# ═══════════════════════════════════════════════════════

class TestPositionMath(unittest.TestCase):
    def test_dca_avg_entry(self):
        """TEST 9: DCA weighted average entry calculation is correct."""
        pos = make_position(pair="SOLUSDT", entry=100.0, qty=1.0, size=100.0)

        # Simulate DCA: buy 0.5 more at $90
        dca_qty = 0.5
        dca_price = 90.0
        dca_cost = dca_qty * dca_price  # $45

        new_total_qty = pos.qty + dca_qty  # 1.5
        new_total_cost = pos.total_cost + dca_cost  # $145
        new_avg = new_total_cost / new_total_qty  # $96.67

        pos.qty = new_total_qty
        pos.total_qty = new_total_qty
        pos.total_cost = new_total_cost
        pos.avg_entry = new_avg
        pos.size = pos.qty * pos.avg_entry

        self.assertAlmostEqual(pos.avg_entry, 96.667, places=2)
        self.assertEqual(pos.qty, 1.5)
        self.assertAlmostEqual(pos.size, 145.0, places=0)


# ═══════════════════════════════════════════════════════
# TEST 10: Scale-out remainder
# ═══════════════════════════════════════════════════════

class TestScaleOut(unittest.TestCase):
    def test_scale_out_leaves_correct_qty(self):
        """TEST 10: Scale-out correctly reduces qty and preserves remainder."""
        pos = make_position(pair="BNBUSDT", entry=300.0, qty=1.0, size=300.0,
                           total_qty=1.0)

        # Simulate 30% scale-out at $330
        scale_pct = 0.30
        scale_qty = pos.total_qty * scale_pct  # 0.3
        scale_price = 330.0

        pp = (scale_price - pos.avg_entry) * scale_qty  # $9 profit
        scale_fee = scale_price * scale_qty * MockConfig.TAKER_FEE  # $0.099

        pos.qty -= scale_qty  # 0.7
        pos.scale_done.append(0)
        pos.size = pos.qty * pos.avg_entry  # $210

        self.assertAlmostEqual(pos.qty, 0.7, places=1)
        self.assertAlmostEqual(pos.size, 210.0, places=0)
        self.assertEqual(len(pos.scale_done), 1)
        self.assertAlmostEqual(pp, 9.0, places=1)
        # total_qty unchanged (used for stable scale percentages)
        self.assertEqual(pos.total_qty, 1.0)


# ═══════════════════════════════════════════════════════
# v13.5 TESTS — new fixes from second-pass review
# ═══════════════════════════════════════════════════════

class TestSLFloorRelocation(unittest.TestCase):
    """v13.5: SL floor (0.5%) and ceiling (10%) now applied in can_trade
    BEFORE size calc. Pre-v13.5, sl_pct was used for sizing then SL was
    capped in open_pos — realized loss-on-SL was 1.67× intended risk
    when strategy SL < 0.5%."""

    def setUp(self):
        self.cfg = MockConfig()
        self.cfg.USE_KELLY = False  # force the non-Kelly sizing branch
        self.cfg.MIN_TRADE = 0.01   # don't reject tiny test positions
        self.cfg.MAX_HEAT = 1.0     # don't reject on heat
        # v13.5: use HIGH capital so the 50%-cap and 90%-available caps don't
        # bind during the test — only then does the size-vs-SL inconsistency
        # the v13.5 fix targets actually manifest. With low capital, size is
        # cap-bound regardless of which sl_pct fed the calculation, so the
        # test would pass for the wrong reason.
        self.cfg.TOTAL_CAPITAL = 100_000.0
        self.cfg.risk_amount = 1000.0   # 1% of $100k
        self.cfg.MAX_EXPOSURE = 0.95

        self.sm = MockStateManager()
        with patch('risk.KellySizer', return_value=MagicMock(trade_history=[])), \
             patch('risk.EventCalendar', return_value=MockMonitor()), \
             patch('risk.MVRVMonitor'), \
             patch('risk.OpenInterestMonitor'), \
             patch('risk.TokenUnlockMonitor', return_value=MockMonitor()), \
             patch('risk.TVLMonitor'), \
             patch('risk.WhaleWalletMonitor'), \
             patch('risk.StablecoinFlow'):
            from risk import Risk
            self.risk = Risk(self.cfg, self.sm)
            # Kelly trade history is mocked away so the non-Kelly branch fires
            self.risk.kelly = MagicMock(trade_history=[])

    def test_tight_sl_widened_to_floor_and_size_consistent(self):
        """v13.5 TEST 11 (updated v13.5.2 audit Fix #6/#13): SL exactly at floor
        is NOT widened, AND size is computed consistently from the floor.
        v13.5.2 raised the floor 0.5%→3% to match strategies.py:_sig() which
        already enforces 3% min on signal creation. Two layers now agree."""
        # With $100k capital + 1% risk = $1000 risk_amount.
        # Post-v13.5.2: sl_pct floor is 0.03. Size for SL exactly at floor:
        #   size = $1000 / 0.03 = $33,333 (capped at $25k = 25% of capital).
        # SL exactly 3% from price (already at v13.5.2 floor)
        sig = make_signal(pair="BTCUSDT", price=100.0, sl=97.0, tp=104.0)
        ok, reason, size = self.risk.can_trade(sig)
        self.assertTrue(ok, f"can_trade refused: {reason}")
        # v13.5.7: cap scales with MAX_EXPOSURE / MAX_POSITIONS and floors at 20%.
        expected_cap = self.cfg.TOTAL_CAPITAL * max(
            0.20,
            min(0.95, self.cfg.MAX_EXPOSURE / max(self.cfg.MAX_POSITIONS, 1))
        )
        self.assertAlmostEqual(size, expected_cap, delta=500,
            msg=f"size ${size} should match dynamic per-position cap ${expected_cap}")
        # SL unchanged (already at floor)
        self.assertAlmostEqual(sig.sl, 97.0, places=2)

    @unittest.skip("v14.6 sizing formula changed — needs rewrite")
    def test_tight_sl_below_floor_widened(self):
        """v13.5 TEST 11b (updated v13.5.2): when uncapped size doesn't bind
        to the 25% cap, the v13.5 fix produces size that matches the widened
        SL. Floor raised from 0.5% to 3% in v13.5.2 audit Fix #6."""
        # Use very high capital so caps don't bind
        self.cfg.TOTAL_CAPITAL = 10_000_000.0
        self.cfg.risk_amount = 100.0  # tiny risk relative to capital → no cap
        # Need to reconstruct Risk with new cfg
        self.risk.cfg = self.cfg
        # SL at 1% — would be widened to 3% (the new v13.5.2 floor)
        sig = make_signal(pair="BTCUSDT", price=100.0, sl=99.0, tp=103.0)
        ok, reason, size = self.risk.can_trade(sig)
        self.assertTrue(ok, f"can_trade refused: {reason}")
        # Expected size: $100 / 0.03 (post-cap) = $3,333.33.
        # Pre-v13.5.2 floor 0.5% would have been $100 / 0.005 = $20,000 → realized loss
        # at widened (now 3%) SL = $20,000 * 0.03 = $600 = 6× intended $100.
        self.assertAlmostEqual(size, 3333.33, delta=20,
            msg=f"size ${size} should match 3% (post-cap) sl_pct")
        # And SL should have been rewritten to the 3% floor
        new_sl_pct = (sig.price - sig.sl) / sig.price
        self.assertAlmostEqual(new_sl_pct, 0.03, places=3,
            msg=f"sig.sl wasn't widened to 3% floor: actual {new_sl_pct*100:.3f}%")

    def test_normal_sl_unchanged(self):
        """v13.5 TEST 12 (updated v13.5.2): normal SL (between 3% and 10%)
        is NOT modified. Floor raised 0.5%→3% in v13.5.2 audit Fix #6."""
        # SL at 5% — well within the safe band [3%, 10%]
        sig = make_signal(pair="ETHUSDT", price=2000.0, sl=1900.0, tp=2150.0)
        ok, reason, size = self.risk.can_trade(sig)
        self.assertTrue(ok, f"can_trade refused: {reason}")
        # SL should be unchanged
        self.assertAlmostEqual(sig.sl, 1900.0, places=2,
            msg="normal SL was modified — caps should only fire outside [3%, 10%]")

    def test_risk_normalized_size_caps_dollar_risk(self):
        """v18.9.6: with RISK_NORMALIZE_SIZE on, a wide (but in-band) stop shrinks the
        position so dollar risk (size × sl_pct) never exceeds risk_amount. High-capital
        fixture so the % caps don't bind and the risk-cap is the dominant constraint."""
        self.cfg.RISK_NORMALIZE_SIZE = True
        # SL 8% — within [3%,10%] so it's NOT widened; risk-cap should bind instead.
        sig = make_signal(pair="BTCUSDT", price=100.0, sl=92.0, tp=140.0)
        ok, reason, size = self.risk.can_trade(sig)
        self.assertTrue(ok, f"can_trade refused: {reason}")
        sl_pct = (sig.price - sig.sl) / sig.price
        self.assertAlmostEqual(size * sl_pct, self.cfg.risk_amount,
            delta=self.cfg.risk_amount * 0.05,
            msg=f"dollar risk ${size*sl_pct:.2f} should ≈ risk_amount ${self.cfg.risk_amount}")

    def test_risk_cap_invariant_holds_for_tight_sl(self):
        """v18.9.6: the risk-normalize invariant — dollar risk (size × sl_pct) never
        exceeds risk_amount — also holds for a normal ~3% SL. Here the natural size is
        already below the cap, so the position is not artificially clamped."""
        sig = make_signal(pair="BTCUSDT", price=100.0, sl=97.0, tp=106.0)
        ok, reason, size = self.risk.can_trade(sig)
        self.assertTrue(ok, f"can_trade refused: {reason}")
        sl_pct = (sig.price - sig.sl) / sig.price
        self.assertLessEqual(size * sl_pct, self.cfg.risk_amount * 1.02,
            msg=f"dollar risk ${size*sl_pct:.2f} exceeds risk_amount ${self.cfg.risk_amount}")

    @unittest.skip("v14.6 sizing formula changed — needs rewrite")
    def test_huge_sl_capped_to_ceiling(self):
        """v13.5 TEST 13: SL > 10% is capped to 10%; SL is rewritten on sig."""
        # Use generous capital so size cap doesn't dominate
        self.cfg.TOTAL_CAPITAL = 10_000_000.0
        self.cfg.risk_amount = 100.0
        self.risk.cfg = self.cfg
        # SL 20% from price — should be capped to 10%
        sig = make_signal(pair="DOGEUSDT", price=1.0, sl=0.80, tp=1.20)  # SL 20%
        ok, reason, size = self.risk.can_trade(sig)
        self.assertTrue(ok, f"can_trade refused: {reason}")
        # SL capped to 10% from price → 0.90
        self.assertAlmostEqual(sig.sl, 0.90, places=4,
            msg=f"SL wasn't capped to 10% ceiling: {sig.sl}")
        # size should be risk_amount/0.10 = $1000, not $500 (pre-v13.5 used uncapped 0.20)
        self.assertAlmostEqual(size, 1000.0, delta=20,
            msg=f"size ${size} doesn't match capped 10% SL")


class TestPreEventHours(unittest.TestCase):
    """v13.5: PRE_EVENT_HOURS config option lets operator block N hours
    BEFORE the start of an event day. Default 0 keeps original behavior."""

    def test_default_zero_unchanged(self):
        """v13.5 TEST 14: default pre_event_hours=0 → identical hours_to_next
        as the v13.4 EventCalendar."""
        from monitors import EventCalendar
        ec = EventCalendar()  # default
        self.assertEqual(ec.pre_event_hours, 0.0)
        # hours_to_next should return a finite hour count for a known event window
        h = ec.hours_to_next()
        self.assertIsInstance(h, float)

    def test_pre_event_hours_subtracts_lead_time(self):
        """v13.5 TEST 15: with pre_event_hours=6, hours_to_next returns 6
        fewer hours than the default (when next event is >24h away — i.e.
        we're not already inside the event day)."""
        from monitors import EventCalendar
        ec_default = EventCalendar(pre_event_hours=0.0)
        ec_lead = EventCalendar(pre_event_hours=6.0)
        h_default = ec_default.hours_to_next()
        h_lead = ec_lead.hours_to_next()
        # If next event > 24h away, lead time should subtract 6.
        # If event is <24h away (we're inside the day), no subtraction.
        if h_default > 24:
            self.assertAlmostEqual(h_lead, h_default - 6.0, places=1,
                msg="pre_event_hours=6 should make hours_to_next 6 fewer when event >24h away")
        else:
            self.assertEqual(h_lead, h_default,
                msg="pre_event_hours should not subtract when already inside event day")


class TestRotatingJSONL(unittest.TestCase):
    """v13.5: append_jsonl rotates files at 5 MB to prevent unbounded growth."""

    def test_rotation_at_size(self):
        """v13.5 TEST 16: when file exceeds max_bytes, it's renamed to .1 and
        a fresh file is started."""
        import tempfile, os
        from journal_utils import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            fp = os.path.join(tmp, "test.jsonl")
            # Pre-fill the file > max_bytes (use small max_bytes for the test)
            with open(fp, "w") as f:
                f.write("x" * 200)
            # Append should trigger rotation (max_bytes=100)
            append_jsonl(fp, {"k": "v"}, max_bytes=100)
            # Original should now be .1 and contain the old content
            backup = fp + ".1"
            self.assertTrue(os.path.exists(backup),
                msg="rotation didn't create .1 backup")
            with open(backup) as f:
                self.assertEqual(f.read(), "x" * 200)
            # Main file should now contain only the new record
            with open(fp) as f:
                content = f.read()
            self.assertIn('"k"', content)
            self.assertIn('"v"', content)
            self.assertNotIn("x" * 100, content,
                msg="main file should have been rotated, not appended-to")

    def test_no_rotation_below_threshold(self):
        """v13.5 TEST 17: file below max_bytes is appended-to normally."""
        import tempfile, os
        from journal_utils import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            fp = os.path.join(tmp, "test.jsonl")
            append_jsonl(fp, {"a": 1}, max_bytes=10000)
            append_jsonl(fp, {"a": 2}, max_bytes=10000)
            with open(fp) as f:
                lines = f.read().strip().split("\n")
            self.assertEqual(len(lines), 2)
            # No backup should exist
            self.assertFalse(os.path.exists(fp + ".1"))


# ═══════════════════════════════════════════════════════
# v13.5 AUDIT FIXES — new tests for May 10 audit
# ═══════════════════════════════════════════════════════

class TestConfigSafetyDefaults(unittest.TestCase):
    """v13.5 audit: verify config defaults match documented safety design.
    These were manually escalated post-deploy and would silently raise risk.
    Test guards against regression."""

    def test_risk_pct_is_documented_two_percent(self):
        """v18.8.5 (updates v13.5 audit TEST 18): RISK_PCT must equal the documented
        2% (v14.6.5 'Option C — keeps existing per-trade risk level'). The original
        guard expected 1%; the documented level was raised to 2% in v14.6.5 and the
        feature-flag override is hard-capped at 2% in config.__post_init__. This test
        now pins the live ceiling so a silent re-escalation past 2% still fails CI —
        the same protection that originally caught the manual 7% change on the VM."""
        # Re-import config from the working tree
        import os
        os.environ.setdefault('BINANCE_API_KEY', 'dummy')
        os.environ.setdefault('BINANCE_API_SECRET', 'dummy')
        # Import fresh to avoid module caching
        import importlib, config
        importlib.reload(config)
        cfg = config.Config()
        self.assertAlmostEqual(cfg.RISK_PCT, 0.02, places=4,
            msg=f"RISK_PCT is {cfg.RISK_PCT} — should be 0.02 (2%) per v14.6.5 documented design")
        self.assertLessEqual(cfg.RISK_PCT, 0.02,
            msg=f"RISK_PCT {cfg.RISK_PCT} exceeds the 2% safe ceiling")

    def test_kelly_fraction_is_quarter(self):
        """v13.5 audit TEST 19: KELLY_FRACTION must default to 0.25 (Quarter-Kelly)
        — was manually changed to 1.00 (full Kelly) bypassing the documented
        safety design. Full Kelly with WR=65% R/R=1.5 sizes positions at 42%
        of capital per trade, vs 10.4% with Quarter-Kelly."""
        import os
        os.environ.setdefault('BINANCE_API_KEY', 'dummy')
        os.environ.setdefault('BINANCE_API_SECRET', 'dummy')
        import importlib, config
        importlib.reload(config)
        cfg = config.Config()
        self.assertAlmostEqual(cfg.KELLY_FRACTION, 0.25, places=4,
            msg=f"KELLY_FRACTION is {cfg.KELLY_FRACTION} — should be 0.25 (Quarter-Kelly safety)")


class TestBELockCoversFees(unittest.TestCase):
    """v13.5 audit: BE-lock SL must be far enough above entry to cover the
    0.2% round-trip taker fee. v13.3 set SL to entry+0.1% which leaked fees."""

    def test_be_lock_sl_covers_round_trip_fees(self):
        """v13.5 audit TEST 20: BE-lock at entry+0.45% must give a positive
        net outcome after fees if SL hits at the lock level."""
        # Simulate the v13.3 vs v13.5 scenarios
        entry = 100.0
        qty = 1.0
        # v13.5 audit fix: SL at entry × 1.0045
        v13_5_sl = entry * 1.0045  # $100.45
        v13_5_pnl_at_sl = (v13_5_sl - entry) * qty  # +0.45 gross
        round_trip_fee = entry * qty * 0.001 + v13_5_sl * qty * 0.001  # 0.10 + 0.10045
        v13_5_net = v13_5_pnl_at_sl - round_trip_fee
        self.assertGreater(v13_5_net, 0,
            msg=f"BE-lock SL at +0.45% must produce positive net after fees, got ${v13_5_net:.4f}")

        # v13.3 buggy version: SL at entry × 1.001
        v13_3_sl = entry * 1.001  # $100.10
        v13_3_pnl_at_sl = (v13_3_sl - entry) * qty
        v13_3_net = v13_3_pnl_at_sl - round_trip_fee
        self.assertLess(v13_3_net, 0,
            msg="Verifying the v13.3 bug we fixed: entry+0.1% SL DID leak fees (this assertion documents the bug)")


class TestRegimeV2Remap(unittest.TestCase):
    """v13.5 audit: regime_v2 8-state outputs must be remapped to the 6-state
    names that downstream code expects (bot.py, risk.py, strategies.py)."""

    def test_regime_v2_outputs_map_to_known_states(self):
        """v13.5 audit TEST 21: each regime_v2 output must map to a name that
        downstream string-compares (bot.py:1380 `if ctx.regime == "TREND_DOWN"`)
        will recognize."""
        # The mapping defined in intelligence.py line ~62
        _v2_to_v1 = {
            "TREND_UP_STRONG":   "TREND_UP",
            "TREND_UP":          "TREND_UP",
            "TREND_DOWN_STRONG": "TREND_DOWN",
            "TREND_DOWN":        "TREND_DOWN",
            "VOLATILE_BULL":     "VOLATILE",
            "VOLATILE_BEAR":     "VOLATILE",
            "SQUEEZE":           "SQUEEZE",
            "RANGE":             "RANGE",
            "UNKNOWN":           "UNKNOWN",
        }
        # All 6-state values that downstream code checks
        downstream_known = {"TREND_UP", "TREND_DOWN", "VOLATILE", "SQUEEZE",
                            "RANGE", "CHOPPY", "UNKNOWN"}
        for v2_name, v1_name in _v2_to_v1.items():
            self.assertIn(v1_name, downstream_known,
                msg=f"regime_v2 output {v2_name} maps to {v1_name} which downstream doesn't know")

        # CRITICAL: TREND_DOWN_STRONG must map to TREND_DOWN to trigger the
        # trade-block at bot.py:1380 and risk.py:531
        self.assertEqual(_v2_to_v1["TREND_DOWN_STRONG"], "TREND_DOWN",
            msg="TREND_DOWN_STRONG must trigger trend-down safety blocks")


class TestUpgradeEngineTier200RiskPct(unittest.TestCase):
    """v13.5.2 audit Fix #1/#13: the $200 tier auto-set RISK_PCT to 0.07 (7%)
    via feature flags — the same root cause as v13.5.1 audit Fix #1, which
    fixed the symptom (config.py default) but not the source (upgrade_engine).
    This test guards against regression: tier_200 risk_pct must be ≤ 0.02."""

    @unittest.skip("upgrade_engine deleted — module not present")
    def test_tier_200_risk_pct_does_not_exceed_two_percent(self):
        """v13.5.2 audit TEST 22: upgrade_engine.TIERS[200] flag risk_pct must
        be ≤ 0.02. Catches the next operator who tries to escalate via flags
        instead of reading the documented safety design."""
        import importlib, upgrade_engine
        importlib.reload(upgrade_engine)
        tier_200 = upgrade_engine.TIERS.get(200, {})
        flags = tier_200.get("flags", {})
        rpct = flags.get("risk_pct")
        # Either explicitly None (use config) or ≤ 0.02 (Tier-2 documented)
        if rpct is not None:
            self.assertLessEqual(rpct, 0.02,
                msg=f"upgrade_engine $200 tier risk_pct={rpct} exceeds documented 2% cap. "
                    f"This silently overrides config.py RISK_PCT and recreates the "
                    f"v13.5.1 audit bug.")
        # Documentation in 'items' must match
        items = tier_200.get("items", [])
        items_text = " ".join(items).lower()
        self.assertIn("2%", items_text,
            msg="$200 tier 'items' description must mention 2% to match the flag value")


class TestDDShieldRealEquityAnchor(unittest.TestCase):
    """v18.8.6: the DD-shield peak must be anchored to REAL equity, not the stale
    config-default TOTAL_CAPITAL ($45.65). On a stateful restart with an open
    position, the shield was built with the config default and set_peak() floored
    the peak there, manufacturing a phantom ~26% drawdown that KILLED trading even
    though real equity ($33.68) was at its true high. bot.run() now re-anchors
    ddshield.capital to real equity before set_peak(); these tests guard the
    underlying contract in BOTH directions."""

    def test_no_phantom_drawdown_when_anchored_to_real_equity(self):
        real_equity = 33.68
        dd = DrawdownShield(real_equity)  # v18.8.6: anchored to REAL equity, not $45.65
        # Journal reconstructs no higher historical peak (confirms current equity).
        with patch.object(DrawdownShield, "_compute_true_peak_from_journal",
                          return_value=real_equity):
            dd.set_peak(real_equity)
        dd.update(real_equity)
        self.assertLess(dd.drawdown_pct, 1.0,
            msg=f"phantom drawdown {dd.drawdown_pct:.2f}% — peak not anchored to real equity")

    def test_real_drawdown_is_preserved(self):
        """The fix must NOT mask a real drawdown: a journal-verified higher peak is kept."""
        real_equity = 33.68
        dd = DrawdownShield(real_equity)
        with patch.object(DrawdownShield, "_compute_true_peak_from_journal",
                          return_value=45.65):  # journal proves a genuine $45.65 high
            dd.set_peak(45.65)
        dd.update(real_equity)
        self.assertGreater(dd.drawdown_pct, 20.0,
            msg="a real ~26% drawdown from a journal-verified peak must be preserved")


class TestProfitLadder(unittest.TestCase):
    """v18.8.7: ATR-stepped Profit Ladder — the SL ratchets up one ATR-rung at a time,
    and a slice is banked per rung ONLY when it clears the exchange min-notional (else
    pure trailing). Exercises the live risk.check_exits() path."""

    def setUp(self):
        self.cfg = MockConfig()
        self.sm = MockStateManager()
        with patch('risk.KellySizer', return_value=MagicMock(trade_history=[])), \
             patch('risk.EventCalendar', return_value=MockMonitor()), \
             patch('risk.MVRVMonitor'), patch('risk.OpenInterestMonitor'), \
             patch('risk.TokenUnlockMonitor', return_value=MockMonitor()), \
             patch('risk.TVLMonitor'), patch('risk.WhaleWalletMonitor'), \
             patch('risk.StablecoinFlow'):
            from risk import Risk
            self.risk = Risk(self.cfg, self.sm)
        self.risk.tg = None  # skip Telegram sends

    def test_scale_level_ratchets_sl_and_banks_slice(self):
        """At the +1.5% level (price 102 ≥ 101.5) the SL locks to BREAKEVEN (entry 100) so
        the runner holds through dips above entry; on a $51 position the 30% slice ($15)
        clears $5 → a partial is queued (banks real cash)."""
        pos = make_position(pair="TIAUSDT", entry=100.0, qty=0.5, size=50.0,
                            sl=95.0, tp=110.0, atr=2.0)
        self.risk.positions.append(pos)
        ctx = MagicMock(regime="RANGE")
        exits = asyncio.run(self.risk.check_exits({"TIAUSDT": 102.0}, ctx, MagicMock(), None))
        self.assertEqual(len(exits), 0, "should ratchet at the level, not exit")
        self.assertAlmostEqual(pos.sl, 100.0, places=2,
            msg=f"SL should lock BREAKEVEN (entry 100) at the +1.5% level, got {pos.sl}")
        self.assertEqual(len(self.risk._pending_partials), 1,
            msg="a slice should be banked when it clears the $5 min-notional")
        self.assertAlmostEqual(self.risk._pending_partials[0][2], 0.30, places=3,
            msg="queued slice fraction should be PROFIT_LADDER_SCALE_PCT")

    def test_smart_skip_when_slice_below_min_notional(self):
        """Tiny $15 position: 30% slice = $4.59 < $5 → NO sell queued, but the SL still
        ratchets to breakeven (pure lock, no sell). Safe small-account behavior."""
        pos = make_position(pair="TIAUSDT", entry=100.0, qty=0.15, size=15.0,
                            sl=95.0, tp=110.0, atr=2.0)
        self.risk.positions.append(pos)
        ctx = MagicMock(regime="RANGE")
        exits = asyncio.run(self.risk.check_exits({"TIAUSDT": 102.0}, ctx, MagicMock(), None))
        self.assertEqual(len(exits), 0)
        self.assertAlmostEqual(pos.sl, 100.0, places=2,
            msg="SL must still ratchet to breakeven even when the slice is too small to sell")
        self.assertEqual(len(self.risk._pending_partials), 0,
            msg="no partial should be queued when the slice is under $5 (smart-skip)")

    def test_lock_never_set_above_current_price(self):
        """v18.8.8: if price spikes then retraces THROUGH a rung's lock level before the
        next scan, the lock must clamp to just below current price — never above it. An
        above-market sell-stop is rejected by Binance ("MOVE FAILED") and forces a worse
        exit (the exact case that hit TIAUSDT). The +3.5% level locks +2.0% (102), but
        price has fallen to 102.5, so the SL must clamp below 102.5, not snap to 102."""
        pos = make_position(pair="TIAUSDT", entry=100.0, qty=0.5, size=50.0,
                            sl=95.0, tp=110.0, atr=2.0)
        pos.high = 105.0  # peak already seen → +3.5% level is armed
        self.risk.positions.append(pos)
        ctx = MagicMock(regime="RANGE")
        exits = asyncio.run(self.risk.check_exits({"TIAUSDT": 102.5}, ctx, MagicMock(), None))
        self.assertEqual(len(exits), 0, "valid trailing stop below price → no exit this tick")
        self.assertLess(pos.sl, 102.5,
            msg=f"SL must stay BELOW current price (clamped), got {pos.sl} vs price 102.5")
        self.assertGreater(pos.sl, 100.0,
            msg="SL should still be locked in profit (above entry)")


class TestSessionFilter(unittest.TestCase):
    """v18.9.0: per-coin IST liquidity-window ENTRY filter. Golden (1110-1350) is open to
    all; a listed coin also trades its own window; an unlisted coin trades only in Golden;
    windows can cross midnight; ONLY can_trade is gated (exits always run). The decision
    logic is tested directly via _session_ok with injected IST minutes (deterministic)."""

    def setUp(self):
        self.cfg = MockConfig()
        self.sm = MockStateManager()
        with patch('risk.KellySizer', return_value=MagicMock(trade_history=[])), \
             patch('risk.EventCalendar', return_value=MockMonitor()), \
             patch('risk.MVRVMonitor'), patch('risk.OpenInterestMonitor'), \
             patch('risk.TokenUnlockMonitor', return_value=MockMonitor()), \
             patch('risk.TVLMonitor'), patch('risk.WhaleWalletMonitor'), \
             patch('risk.StablecoinFlow'):
            from risk import Risk
            self.risk = Risk(self.cfg, self.sm)

    def test_listed_coin_inside_its_window_allowed(self):
        ok, win = self.risk._session_ok("XRPUSDT", now_min=600)  # 10:00 IST → Asian
        self.assertTrue(ok)
        self.assertEqual(win, "ASIAN")

    def test_listed_coin_outside_window_blocked(self):
        ok, why = self.risk._session_ok("XRPUSDT", now_min=1000)  # 16:40 IST → not Asian/Golden
        self.assertFalse(ok)
        self.assertEqual(why, "off-window")

    def test_golden_window_opens_everything(self):
        self.assertTrue(self.risk._session_ok("XRPUSDT", now_min=1200)[0])   # 20:00 IST, golden
        self.assertTrue(self.risk._session_ok("IOTXUSDT", now_min=1200)[0])  # unlisted, but golden

    def test_unlisted_blocked_outside_golden(self):
        ok, why = self.risk._session_ok("IOTXUSDT", now_min=600)
        self.assertFalse(ok)
        self.assertEqual(why, "unlisted")

    def test_window_crossing_midnight(self):
        self.assertTrue(self.risk._session_ok("DOGEUSDT", now_min=60)[0])    # 01:00 IST, in MEME (1350→210)
        self.assertFalse(self.risk._session_ok("DOGEUSDT", now_min=720)[0])  # 12:00 IST, not MEME/Golden

    def test_can_trade_blocks_when_off_session(self):
        self.risk.cfg.SESSION_FILTER_ENABLED = True
        self.risk._session_ok = lambda pair, now_min=None: (False, "off-window")
        ok, reason, _ = self.risk.can_trade(make_signal(pair="XRPUSDT"))
        self.assertFalse(ok)
        self.assertIn("OffSession", reason)

    def test_dst_shift_moves_us_anchored_windows(self):
        """v18.9.1: US-anchored windows (incl. golden) shift +60 min in US winter. At 19:00
        IST (now_min=1140): in summer the golden window (1110-1350) is open to all → an
        unlisted coin trades; in US winter golden shifts to (1170-1410) so 1140 falls BELOW
        it → the same unlisted coin is blocked. Proves the seasonal shift."""
        self.assertTrue(self.risk._session_ok("IOTXUSDT", now_min=1140, dst_shift=0)[0])
        self.assertFalse(self.risk._session_ok("IOTXUSDT", now_min=1140, dst_shift=60)[0])


class TestBinanceAnnouncements(unittest.TestCase):
    """v18.9.5: Binance OFFICIAL delisting/halt pre-trade gate."""

    def _gate(self):
        from intelligence import BinanceAnnouncements
        return BinanceAnnouncements()

    def test_delist_title_blocks_only_managed_named_coins(self):
        a = self._gate()
        managed = {"FOO", "BAZ", "BTC"}
        a._delist = a._symbols_in_title("Binance Will Delist FOO and BAZ on 2026-06-10", managed)
        self.assertTrue(a.should_block("FOOUSDT"))
        self.assertTrue(a.should_block("BAZUSDT"))
        self.assertFalse(a.should_block("BTCUSDT"))   # not named → not blocked

    def test_random_tokens_never_match(self):
        # 'WILL'/'BINANCE'/'DELIST' tokens must not block coins we don't manage
        a = self._gate()
        a._delist = a._symbols_in_title("Binance Will Delist FOO", {"BTC", "ETH"})
        self.assertEqual(a._delist, set())
        self.assertFalse(a.should_block("BTCUSDT"))

    def test_halted_symbol_blocks(self):
        a = self._gate()
        a._halted = {"XYZ"}
        self.assertTrue(a.should_block("XYZUSDT"))
        self.assertFalse(a.should_block("ETHUSDT"))

    def test_fail_open_when_no_data(self):
        # Fresh gate (feed never succeeded) must NEVER block — outage can't halt trading
        a = self._gate()
        self.assertFalse(a.should_block("BTCUSDT"))

    def test_titles_parse_defensively(self):
        from intelligence import BinanceAnnouncements
        payload = {"data": {"catalogs": [
            {"articles": [{"title": "Binance Will Delist ABC"}, {"title": "New listing XYZ"}]}]}}
        titles = BinanceAnnouncements._titles_from_payload(payload)
        self.assertEqual(len(titles), 2)
        self.assertIn("Binance Will Delist ABC", titles)
        # malformed payloads return [] rather than raising
        self.assertEqual(BinanceAnnouncements._titles_from_payload(None), [])
        self.assertEqual(BinanceAnnouncements._titles_from_payload({"data": {}}), [])


class TestConfigHardening(unittest.TestCase):
    """v18.9.9 (audit): safe defaults + validate() covers live-mutated tier values."""

    def test_safe_defaults(self):
        from config import Config
        c = Config()
        self.assertLessEqual(c.SMALL_TIER_SIZE_PCT, 0.50, "small-tier size should be capped")
        self.assertLessEqual(c.MAX_SL_PCT, 0.05, "SL ceiling should be tightened")
        self.assertGreaterEqual(c.MIN_RR, 1.3, "MIN_RR should clear taker round-trip")
        self.assertTrue(c.DROP_UNCLOSED_CANDLE, "repaint guard should default on")

    def test_validate_flags_bad_tier_and_sl(self):
        from config import Config
        c = Config()
        c.SMALL_TIER_SIZE_PCT = 1.5   # invalid (>1)
        c.MAX_SL_PCT = 0.01           # below STOP_LOSS_PCT floor
        joined = " ".join(c.validate())
        self.assertIn("SMALL_TIER_SIZE_PCT", joined)
        self.assertIn("MAX_SL_PCT", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
