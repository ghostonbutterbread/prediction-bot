"""OpenRouter API client — direct integration for LLM researcher."""

import json
import logging
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class OpenRouterClient:
    """
    Direct OpenRouter API client. No sub-agent spawning — just HTTP calls.

    Features:
    - Configurable model (from config.yaml)
    - Response caching (avoid duplicate calls)
    - Daily call budget enforcement
    - Automatic fallback to cheaper model on errors
    - Full cost tracking
    """

    def __init__(self, config: dict):
        self.api_key = self._load_api_key(config)
        self.model = config.get("model", "google/gemini-2.5-flash")
        self.fallback_model = config.get("fallback_model", "anthropic/claude-sonnet-4")
        self.max_tokens = config.get("max_tokens", 1024)
        self.temperature = config.get("temperature", 0.3)
        self.daily_budget = config.get("daily_call_budget", 20)
        self.cache_ttl = config.get("cache_ttl_minutes", 30) * 60  # convert to seconds

        # State
        self.calls_today = 0
        self.total_cost = 0.0
        self.cache = {}  # hash -> (response, timestamp)
        self.daily_reset = datetime.now(timezone.utc).date()

        # Stats file
        self.stats_file = Path("data/researcher_stats.json")
        self._load_stats()

        logger.info(f"OpenRouter researcher: model={self.model}, budget={self.daily_budget}/day")

    def _load_api_key(self, config: dict) -> str:
        """Load API key from env var specified in config."""
        import os
        env_name = config.get("api_key_env", "OPENROUTER_API_KEY")
        key = os.getenv(env_name, "")
        if not key:
            logger.warning(f"⚠️ {env_name} not set — researcher disabled")
        return key

    def _load_stats(self):
        """Load persistent stats."""
        if self.stats_file.exists():
            try:
                with open(self.stats_file) as f:
                    stats = json.load(f)
                # Reset if new day
                if stats.get("date") == str(datetime.now(timezone.utc).date()):
                    self.calls_today = stats.get("calls", 0)
                    self.total_cost = stats.get("cost", 0.0)
                else:
                    self.calls_today = 0
                    self.total_cost = 0.0
            except Exception:
                pass

    def _save_stats(self):
        """Persist stats to disk."""
        self.stats_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.stats_file, "w") as f:
            json.dump({
                "date": str(datetime.now(timezone.utc).date()),
                "calls": self.calls_today,
                "cost": self.total_cost,
                "model": self.model,
            }, f, indent=2)

    def _cache_key(self, prompt: str, system: str) -> str:
        """Generate cache key from prompt content."""
        content = f"{self.model}:{system}:{prompt}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def is_available(self) -> bool:
        """Check if researcher can make calls."""
        return bool(self.api_key) and self.calls_today < self.daily_budget

    def query(
        self,
        prompt: str,
        system: str = "You are a quantitative analyst. Be concise and analytical.",
        force_model: str = None,
        skip_cache: bool = False,
    ) -> Optional[str]:
        """
        Send a query to OpenRouter. Returns response text or None.

        Args:
            prompt: The user prompt
            system: System prompt
            force_model: Override the default model for this call
            skip_cache: Bypass cache (for time-sensitive queries)
        """
        if not self.api_key:
            logger.debug("No API key — skipping researcher call")
            return None

        # Check budget
        if self.calls_today >= self.daily_budget:
            logger.debug(f"Daily budget reached ({self.daily_budget}) — skipping")
            return None

        # Check cache
        if not skip_cache:
            cache_key = self._cache_key(prompt, system)
            if cache_key in self.cache:
                cached_response, cached_at = self.cache[cache_key]
                if time.time() - cached_at < self.cache_ttl:
                    logger.debug("Cache hit — reusing previous analysis")
                    return cached_response

        model = force_model or self.model

        try:
            response = self._make_request(prompt, system, model)
            self.calls_today += 1

            # Cache the response
            if not skip_cache:
                self.cache[cache_key] = (response, time.time())

            self._save_stats()
            return response

        except Exception as e:
            logger.warning(f"OpenRouter call failed ({model}): {e}")
            # Try fallback model
            if model != self.fallback_model:
                try:
                    logger.info(f"Trying fallback: {self.fallback_model}")
                    response = self._make_request(prompt, system, self.fallback_model)
                    self.calls_today += 1
                    self._save_stats()
                    return response
                except Exception as e2:
                    logger.error(f"Fallback also failed: {e2}")
            return None

    def _make_request(self, prompt: str, system: str, model: str) -> str:
        """Make the actual HTTP request to OpenRouter."""
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/ryushe/prediction-bot",
                    "X-Title": "Prediction Bot Researcher",
                },
                json={
                    "model": model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()

            # Track cost if available
            usage = data.get("usage", {})
            if "total_cost" in usage:
                self.total_cost += usage["total_cost"]

            content = data["choices"][0]["message"]["content"]
            logger.info(
                f"Researcher call: model={model} tokens={usage.get('total_tokens', '?')} "
                f"calls_today={self.calls_today}/{self.daily_budget}"
            )
            return content

    def get_stats(self) -> dict:
        """Return current stats."""
        return {
            "model": self.model,
            "calls_today": self.calls_today,
            "daily_budget": self.daily_budget,
            "total_cost": self.total_cost,
            "cache_entries": len(self.cache),
            "available": self.is_available(),
        }
