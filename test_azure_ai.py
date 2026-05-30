import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# Ensure env vars are loaded if dotenv is installed, else relies on OS env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# We can import AzureOpenAIIntelligence if we are in the correct directory
try:
    from intelligence import AzureOpenAIIntelligence
except ImportError:
    print("Please run this script from inside the binbot directory.")
    sys.exit(1)

def main():
    print("--- Azure OpenAI Integration Test ---")
    
    # Check if the user exported the environment variables. 
    # If not, for this test, we can try to fall back to the defaults provided by the user.
    # v18.8.6 SECURITY: creds come from env ONLY — no hardcoded fallback (this file
    # previously embedded a live AZURE_OPENAI_KEY). Set them in your gitignored .env.
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    key = os.environ.get("AZURE_OPENAI_KEY", "")
    if not endpoint or not key:
        print("[SKIP] Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY in your environment to run this test.")
        sys.exit(0)
    
    # We enforce setting them in env to ensure the bot context has them.
    os.environ["AZURE_OPENAI_ENDPOINT"] = endpoint
    os.environ["AZURE_OPENAI_KEY"] = key
    os.environ["AZURE_OPENAI_MODEL"] = "gpt-oss-120b"
        
    print(f"Endpoint: {endpoint}")
    print("Initializing Azure OpenAI Intelligence...")
    
    ai = AzureOpenAIIntelligence()
    
    if ai.status() == "OAI:OFF":
        print("[FAIL] Failed to initialize. Check your API keys and endpoint.")
        sys.exit(1)
        
    print("[SUCCESS] Initialization successful!")
    print("\nSending a test prompt: Bullish news for BTCUSDT...")
    
    headlines = [
        "Bitcoin surges past $80,000 as institutional adoption accelerates.",
        "Major ETF inflows push BTC to new all-time highs.",
        "Whales accumulate Bitcoin at unprecedented rates."
    ]
    
    score = ai.analyze_news(headlines, "BTCUSDT")
    print(f"\nResulting Score: {score}")
    
    if score > 0.5:
        print("[SUCCESS] Correct sentiment detection (Bullish).")
    else:
        print("[WARN] Warning: Score is not strongly bullish, which is unexpected for this prompt.")
        
    print("\nSending a test prompt: Bearish news for ETHUSDT...")
    headlines = [
        "Ethereum foundation gets hacked for $500M.",
        "SEC declares ETH a security and bans trading.",
        "Massive sell-off hits crypto markets."
    ]
    
    score = ai.analyze_news(headlines, "ETHUSDT")
    print(f"\nResulting Score: {score}")
    
    if score < -0.5:
        print("[SUCCESS] Correct sentiment detection (Bearish).")
    else:
        print("[WARN] Warning: Score is not strongly bearish, which is unexpected for this prompt.")

if __name__ == "__main__":
    main()
