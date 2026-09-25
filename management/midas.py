# MIDAS — Greek hero — everything turns to gold
# Finance: budgets, expenses, transactions, financial awareness

import json
import os
import requests
from datetime import datetime
from core.marduk import OdinModule


class Midas(OdinModule):
    MODULE_NAME = "MIDAS"
    LAYER = "MANAGEMENT"

    def __init__(self, config: dict):
        super().__init__(config)
        self._ledger_path = "data/knowledge/ledger.json"
        self._ledger: list = self._load()

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "log_expense",
                "description": "Log a new expense or transaction",
                "parameters": {
                    "amount": {"type": "number", "description": "Amount spent"},
                    "category": {"type": "string", "description": "Category (e.g. food, transport, bills)"},
                    "note": {"type": "string", "description": "Optional description"}
                },
                "required": ["amount", "category"]
            },
            {
                "name": "get_spending_summary",
                "description": "Get a summary of recent spending",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "set_budget",
                "description": "Set a monthly budget target",
                "parameters": {
                    "amount": {"type": "number", "description": "Monthly budget in your currency"}
                },
                "required": ["amount"],
                "internal_only": True
            },
            {
                "name": "get_budget_status",
                "description": "Check how much of the monthly budget has been spent",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "convert_currency",
                "description": "Convert money between currencies at current ECB rates. Uses Frankfurter (free, no key).",
                "parameters": {
                    "amount": {"type": "number", "description": "Amount to convert"},
                    "from_currency": {"type": "string", "description": "Source currency code (USD, EUR, INR, GBP, etc.)"},
                    "to_currency": {"type": "string", "description": "Target currency code"}
                },
                "required": ["amount", "from_currency", "to_currency"]
            },
            {
                "name": "crypto_price",
                "description": "Get current price of a cryptocurrency in USD and INR. Uses CoinGecko (free, no key).",
                "parameters": {
                    "coin": {"type": "string", "description": "Coin name or symbol (bitcoin, eth, dogecoin, etc.)"}
                },
                "required": ["coin"]
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "log_expense": self._log_expense,
            "get_spending_summary": self._summary,
            "set_budget": self._set_budget,
            "get_budget_status": self._budget_status,
            "convert_currency": self._convert_currency,
            "crypto_price": self._crypto_price,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[MIDAS] Error: {e}"
        return f"[MIDAS] Unknown skill: {skill_name}"

    def _log_expense(self, amount: float = 0, category: str = "", note: str = "") -> str:
        entry = {
            "amount": float(amount),
            "category": category,
            "note": note,
            "date": datetime.now().isoformat()
        }
        self._ledger.append(entry)
        self._save()
        return f"Logged: {category} — {amount}."

    def _summary(self) -> str:
        if not self._ledger:
            return "No expenses logged yet."
        total = sum(e["amount"] for e in self._ledger)
        by_cat: dict[str, float] = {}
        for e in self._ledger:
            by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
        breakdown = ", ".join(f"{k}: {v:.0f}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1]))
        return f"Total spent: {total:.0f}. By category: {breakdown}."

    def _set_budget(self, amount: float = 0) -> str:
        path = "data/knowledge/budget.json"
        with open(path, "w") as f:
            json.dump({"monthly_budget": float(amount)}, f)
        return f"Monthly budget set to {amount}."

    def _budget_status(self) -> str:
        path = "data/knowledge/budget.json"
        if not os.path.exists(path):
            return "No budget set. Use set_budget to configure one."
        with open(path) as f:
            data = json.load(f)
        budget = data.get("monthly_budget", 0)
        now = datetime.now()
        month_entries = [
            e for e in self._ledger
            if e["date"][:7] == now.strftime("%Y-%m")
        ]
        spent = sum(e["amount"] for e in month_entries)
        remaining = budget - spent
        pct = (spent / budget * 100) if budget else 0
        return (f"This month: spent {spent:.0f} of {budget:.0f} budget. "
                f"{pct:.0f}% used. {remaining:.0f} remaining.")

    def _load(self) -> list:
        if os.path.exists(self._ledger_path):
            with open(self._ledger_path) as f:
                return json.load(f)
        return []

    def _save(self):
        os.makedirs(os.path.dirname(self._ledger_path), exist_ok=True)
        with open(self._ledger_path, "w") as f:
            json.dump(self._ledger, f, indent=2)

    # === External free APIs (no key required) ===========================

    def _convert_currency(self, amount: float = 0, from_currency: str = "USD",
                          to_currency: str = "INR") -> str:
        from_c = (from_currency or "USD").upper().strip()
        to_c   = (to_currency or "INR").upper().strip()
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return "Amount must be a number."
        if from_c == to_c:
            return f"{amt:.2f} {from_c} is {amt:.2f} {to_c} — same currency."
        try:
            r = requests.get(
                "https://api.frankfurter.app/latest",
                params={"amount": amt, "from": from_c, "to": to_c},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
            converted = (data.get("rates") or {}).get(to_c)
            if converted is None:
                return f"Could not convert {from_c} to {to_c}. Check currency codes."
            return f"{amt:.2f} {from_c} = {converted:.2f} {to_c}."
        except Exception as e:
            return f"Currency lookup failed: {e}"

    def _crypto_price(self, coin: str = "bitcoin") -> str:
        # CoinGecko's simple-price endpoint accepts the canonical coin id.
        # We pass the user's input as-is; common names like "bitcoin", "eth",
        # "dogecoin" all resolve. Returns USD + INR for an Indian user.
        cid = (coin or "bitcoin").lower().strip()
        # Common symbol → id mapping for the handful that don't match directly.
        aliases = {
            "btc": "bitcoin", "eth": "ethereum", "doge": "dogecoin",
            "sol": "solana", "ada": "cardano", "xrp": "ripple",
            "matic": "polygon", "dot": "polkadot", "ltc": "litecoin",
        }
        cid = aliases.get(cid, cid)
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cid, "vs_currencies": "usd,inr"},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
            entry = data.get(cid)
            if not entry:
                return f"No price data for '{coin}'. Try the full name (e.g. 'bitcoin' not 'BTC')."
            usd = entry.get("usd", 0)
            inr = entry.get("inr", 0)
            return f"{cid.title()}: ${usd:,.2f} USD ({inr:,.0f} INR)."
        except Exception as e:
            return f"Crypto price lookup failed: {e}"
