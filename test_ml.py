import asyncio, time
import numpy as np
from ml import MLPredictor
from models import Candle
from indicators import TA

class DummyTA:
    def rsi(self, c): return 50
    def macd(self, c): return [0], [0], [0]
    def bb(self, c): return 100, 50, 0, 100
    def adx(self, c): return 25
    def vol_ratio(self, c): return 1.0
    def ema(self, c, p): return [c[-1].c]
    def atr(self, c): return 10
    def bb_squeeze(self, c): return False, 0
    def vwap(self, c): return 50

# Generate 600 dummy candles with slightly predictable pattern
candles = []
price = 50.0
for i in range(600):
    # Trend up if i % 20 < 10
    if i % 20 < 10: price += 0.5
    else: price -= 0.3
    c = Candle(ts=i*300000, o=price-0.1, h=price+0.5, l=price-0.5, c=price, v=100)
    candles.append(c)

print("Instantiating MLPredictor...")
ml = MLPredictor(retrain_hours=6)
print(f"ML Ready state before train: {ml._ready}")
print("Training MLPredictor...")
ta = DummyTA()
success = ml.train(candles, ta)
print(f"Training success: {success}")
print(f"ML Ready state after train: {ml._ready}")
print(f"ML Accuracy: {ml.accuracy}")
if success:
    score = ml.predict(candles[-100:], ta)
    print(f"Prediction score: {score}")
