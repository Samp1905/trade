import os
from dotenv import load_dotenv
import ccxt

load_dotenv()

key = os.environ.get("KRAKEN_API_KEY", "")
secret = os.environ.get("KRAKEN_API_SECRET", "")

print("=== Credential Check ===")
print(f"KEY    : length={len(key)}  starts with '{key[:6]}'")
print(f"SECRET : length={len(secret)}  starts with '{secret[:6]}'")

if not key or key == "your_demo_api_key_here":
    print("\nFAIL: KRAKEN_API_KEY is missing or still a placeholder.")
    print("Fix : open ~/.env and paste your real API key from demo-futures.kraken.com")
    exit(1)

if not secret or secret == "your_demo_api_secret_here":
    print("\nFAIL: KRAKEN_API_SECRET is missing or still a placeholder.")
    print("Fix : open ~/.env and paste your real API secret from demo-futures.kraken.com")
    exit(1)

print("\n=== Connecting to Kraken Futures Demo ===")
try:
    exchange = ccxt.krakenfutures({"apiKey": key, "secret": secret})
    exchange.set_sandbox_mode(True)
    balance = exchange.fetch_balance()
    usd = balance.get("USD", {}).get("total", "N/A")
    print(f"PASS: Connected! USD balance = {usd}")
except ccxt.AuthenticationError:
    print("FAIL: authenticationError")
    print("Fix : Your keys are wrong or from the live site (futures.kraken.com).")
    print("      Go to demo-futures.kraken.com → Settings → API Keys → create new keys.")
except ccxt.BaseError as e:
    print(f"FAIL: {e}")
