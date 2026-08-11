import asyncio
import re
import time

from .deepseek_direct import call_single_deepseek_r1, call_single_deepseek_r2
from .mistral_fallback import (
    call_single_mistral_r1,
    call_single_mistral_r2,
    call_single_mistral_r3,
)
from .nvidia_fallback import call_single_nvidia_r1, call_single_nvidia_r2
from .cloudflare_fallback import call_single_cloudflare_r1, call_single_cloudflare_r2
import supabase_client as sc


# -----------------------------------------------------------------------------
# GLOBAL LLM SETTINGS
# -----------------------------------------------------------------------------
# Limit concurrent LLM work so several users cannot exhaust every API key at once.
_LLM_SEMAPHORE = asyncio.Semaphore(5)

_R1_MAX_TOKENS = 8000
_R2_MAX_TOKENS = 8000

# A single provider/model gets this much time before the router moves on.
_LLM_CALL_TIMEOUT = 30

# Absolute ceiling for the complete fallback chain.
_LLM_TOTAL_TIMEOUT_R1 = 120
_LLM_TOTAL_TIMEOUT_R2 = 150

# Circuit-breaker cooldowns.
_COOLDOWN_SECS_429 = 120          # rate limit
_COOLDOWN_SECS_401 = 900          # invalid/unauthorized key: 15 minutes
_COOLDOWN_SECS_404 = 60           # model not found / unavailable
_COOLDOWN_SECS_410 = 60           # obsolete model
_COOLDOWN_SECS_OTHER = 300        # other classified provider failures

# Local circuit state. IMPORTANT: key is provider + model + API key.
_circuit_tripped = {}


# -----------------------------------------------------------------------------
# COOLDOWN HELPERS
# -----------------------------------------------------------------------------
def _get_402_cooldown_secs() -> int:
    """Cooldown until the next daily reset window for billing/payment failures."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0
    )
    cooldown = int((tomorrow - now).total_seconds())
    return max(300, min(cooldown, 86400))


def _make_circuit_key(provider: str, model_id: str, key_label: str) -> str:
    """One breaker per exact provider + model + API-account combination."""
    return f"{provider}_{model_id}_{key_label}"


def _split_circuit_key(circuit_key: str):
    """Split the standard provider_model_key representation safely."""
    try:
        provider, model_id, key_label = circuit_key.rsplit("_", 2)
        return provider, model_id, key_label
    except ValueError:
        return circuit_key, "unknown", "unknown"


# -----------------------------------------------------------------------------
# CIRCUIT BREAKER
# -----------------------------------------------------------------------------
async def _trip_circuit(circuit_key: str, error_type: str):
    """Temporarily remove one exact model/key combination from the rotation."""
    if error_type == "config":
        # Missing environment variable is local configuration, not a global outage.
        _circuit_tripped[circuit_key] = float("inf")
        print(f"[LLM] {circuit_key}: config error; disabled locally")
        return

    if error_type == "429":
        cooldown = _COOLDOWN_SECS_429
    elif error_type == "402":
        cooldown = _get_402_cooldown_secs()
    elif error_type == "401":
        cooldown = _COOLDOWN_SECS_401
    elif error_type == "404":
        cooldown = _COOLDOWN_SECS_404
    elif error_type == "410":
        cooldown = _COOLDOWN_SECS_410
    else:
        cooldown = _COOLDOWN_SECS_OTHER

    provider, model_id, key_label = _split_circuit_key(circuit_key)
    _circuit_tripped[circuit_key] = time.time() + cooldown

    print(
        f"[LLM] Circuit tripped: {circuit_key} "
        f"({error_type}) for {cooldown}s"
    )

    # Telemetry must never break generation.
    if sc.supabase:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: sc.supabase.table("llm_usage").insert({
                        "request_type": "circuit_trip",
                        "provider": provider,
                        "model": model_id,
                        "success": False,
                        "speed_secs": 0,
                    }).execute()
                ),
                timeout=0.8,
            )
        except Exception:
            pass

        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: sc.supabase.rpc(
                        "trip_circuit_breaker",
                        {
                            "p_circuit_key": circuit_key,
                            "p_cooldown_seconds": cooldown,
                        },
                    ).execute()
                ),
                timeout=0.8,
            )
        except Exception:
            # DB circuit persistence is best-effort; local state still works.
            pass


async def _reset_circuit(circuit_key: str):
    """Make one exact provider/model/key combination immediately eligible."""
    _circuit_tripped.pop(circuit_key, None)

    if sc.supabase:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: sc.supabase.rpc(
                        "reset_circuit_breaker",
                        {"p_circuit_key": circuit_key},
                    ).execute()
                ),
                timeout=0.8,
            )
        except Exception:
            pass

    print(f"[LLM] Circuit reset: {circuit_key}")


def _is_tripped(circuit_key: str, db_tripped_keys: set | None = None) -> bool:
    """Check DB + local breaker state and automatically expire local entries."""
    if db_tripped_keys and circuit_key in db_tripped_keys:
        return True

    expires_at = _circuit_tripped.get(circuit_key)
    if expires_at is None:
        return False

    if expires_at == float("inf"):
        return True

    if time.time() >= expires_at:
        _circuit_tripped.pop(circuit_key, None)
        return False

    return True


# -----------------------------------------------------------------------------
# ERROR CLASSIFICATION
# -----------------------------------------------------------------------------
def _get_rate_limit_type(attempts) -> str | None:
    """Classify provider failures so only the failing model/key is cooled down."""
    for att in attempts or []:
        status = str(att.get("status", ""))
        lower = status.lower()

        if re.search(r"\b402\b", status) or "payment required" in lower:
            return "402"

        if (
            re.search(r"\b429\b", status)
            or re.search(r"rate.?limit", status, re.I)
            or "resource_exhausted" in lower
        ):
            return "429"

        if any(x in lower for x in (
            "not configured",
            "api key not",
            "missing_api_key",
            "missing api key",
        )):
            return "config"

        if (
            re.search(r"\b401\b", status)
            or re.search(r"\b403\b", status)
            or any(x in lower for x in (
                "unauthorized",
                "invalid_api_key",
                "invalid api key",
                "forbidden",
            ))
        ):
            return "401"

        if re.search(r"\b404\b", status) or "not found" in lower:
            return "404"

        if re.search(r"\b410\b", status) or "gone" in lower:
            return "410"

    return None


# -----------------------------------------------------------------------------
# MODEL POOLS
# -----------------------------------------------------------------------------
# Every entry is (provider, model_id, API account label).
# The circuit breaker is also keyed by this exact 3-part identity.
#
# DeepSeek is handled as the first dedicated model in the universal chain.
# Mistral/Ministral/Cloudflare/NVIDIA then provide the fallback pool.
#
# Current documented model IDs used here were checked against provider docs:
# DeepSeek V4 Flash, Mistral Medium 3.5, Mistral Large 3, Mistral Small,
# Ministral 14B, Cloudflare Llama 3.3 70B, and NVIDIA Mistral Nemotron.
# -----------------------------------------------------------------------------
POOL_1 = [
    ("mistral", "mistral-medium-3-5", "Key 1"),
    ("mistral", "mistral-medium-3-5", "Key 2"),
    ("mistral", "mistral-medium-3-5", "Key 3"),

    ("mistral", "mistral-medium-latest", "Key 1"),
    ("mistral", "mistral-medium-latest", "Key 2"),
    ("mistral", "mistral-medium-latest", "Key 3"),

    ("mistral", "mistral-large-2512", "Key 1"),
    ("mistral", "mistral-large-2512", "Key 2"),
    ("mistral", "mistral-large-2512", "Key 3"),

    ("mistral", "mistral-large-latest", "Key 1"),
    ("mistral", "mistral-large-latest", "Key 2"),
    ("mistral", "mistral-large-latest", "Key 3"),
]

POOL_2 = [
    ("ministral", "ministral-14b-latest", "Key 1"),
    ("ministral", "ministral-14b-latest", "Key 2"),
    ("ministral", "ministral-14b-latest", "Key 3"),

    ("mistral", "mistral-small-latest", "Key 1"),
    ("mistral", "mistral-small-latest", "Key 2"),
    ("mistral", "mistral-small-latest", "Key 3"),

    ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "Key 1"),
    ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "Key 2"),

    ("nvidia", "mistralai/mistral-nemotron", "Key 1"),
    ("nvidia", "mistralai/mistral-nemotron", "Key 2"),
]


# -----------------------------------------------------------------------------
# CALLER LOOKUP
# -----------------------------------------------------------------------------
# Key labels map to actual environment variables inside the provider modules.
_CALLERS = {
    ("deepseek", "Key 1"): call_single_deepseek_r1,
    ("deepseek", "Key 2"): call_single_deepseek_r2,

    ("mistral", "Key 1"): call_single_mistral_r1,
    ("mistral", "Key 2"): call_single_mistral_r2,
    ("mistral", "Key 3"): call_single_mistral_r3,

    ("ministral", "Key 1"): call_single_mistral_r1,
    ("ministral", "Key 2"): call_single_mistral_r2,
    ("ministral", "Key 3"): call_single_mistral_r3,

    ("nvidia", "Key 1"): call_single_nvidia_r1,
    ("nvidia", "Key 2"): call_single_nvidia_r2,

    ("cloudflare", "Key 1"): call_single_cloudflare_r1,
    ("cloudflare", "Key 2"): call_single_cloudflare_r2,
}


# -----------------------------------------------------------------------------
# ROUND ROBIN
# -----------------------------------------------------------------------------
def _get_start_idx(pool_size: int, offset: int = 0) -> int:
    minute_bucket = int(time.time() // 60)
    return (minute_bucket + offset) % pool_size


_pool1_idx = None
_pool2_idx = None
_rr_initialized = False
_rr_init_lock = asyncio.Lock()
_rr_lock = asyncio.Lock()


async def _ensure_rr_initialized():
    global _pool1_idx, _pool2_idx, _rr_initialized

    if _rr_initialized:
        return

    async with _rr_init_lock:
        if _rr_initialized:
            return

        p1_fallback = _get_start_idx(len(POOL_1), offset=0)
        p2_fallback = _get_start_idx(len(POOL_2), offset=7)

        if sc.supabase:
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: sc.supabase.table("rr_counters")
                        .select("name,counter")
                        .in_("name", ["pool_1_global", "pool_2_global"])
                        .execute()
                    ),
                    timeout=1.5,
                )

                if res.data:
                    for row in res.data:
                        if row["name"] == "pool_1_global":
                            _pool1_idx = int(row["counter"]) % len(POOL_1)
                        elif row["name"] == "pool_2_global":
                            _pool2_idx = int(row["counter"]) % len(POOL_2)

                    print(
                        f"[RR] Resumed from DB — "
                        f"pool1={_pool1_idx}, pool2={_pool2_idx}"
                    )
            except Exception as exc:
                print(f"[RR] DB load failed; using clock seed: {exc}")

        if _pool1_idx is None:
            _pool1_idx = p1_fallback
        if _pool2_idx is None:
            _pool2_idx = p2_fallback

        _rr_initialized = True


async def _get_next_rr_index(pool_type: int) -> int:
    await _ensure_rr_initialized()
    async with _rr_lock:
        return _pool1_idx if pool_type == 1 else _pool2_idx


async def _advance_rr_index(pool_type: int, pool_size: int, winner_idx: int):
    """Start the next request after the model that just succeeded."""
    next_idx = (winner_idx + 1) % pool_size

    global _pool1_idx, _pool2_idx
    async with _rr_lock:
        if pool_type == 1:
            _pool1_idx = next_idx
        else:
            _pool2_idx = next_idx

    if sc.supabase:
        counter_name = "pool_1_global" if pool_type == 1 else "pool_2_global"

        async def _persist():
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: sc.supabase.table("rr_counters")
                        .update({"counter": next_idx})
                        .eq("name", counter_name)
                        .execute()
                    ),
                    timeout=0.8,
                )
            except Exception:
                pass

        asyncio.create_task(_persist())


async def _get_pool_models(pool_type: int) -> list:
    pool = POOL_1 if pool_type == 1 else POOL_2
    idx = await _get_next_rr_index(pool_type)
    return pool[idx:] + pool[:idx]


# -----------------------------------------------------------------------------
# PROVIDER RESOLUTION
# -----------------------------------------------------------------------------
def _get_provider_for_model(model_id: str) -> str:
    base_model_id = model_id.split("|")[0]

    if base_model_id.startswith("deepseek-"):
        return "deepseek"
    if base_model_id.startswith("@cf/"):
        return "cloudflare"
    if (
        base_model_id.startswith("mistralai/")
        or base_model_id.startswith("nvidia/")
        or base_model_id.startswith("meta/")
    ):
        return "nvidia"
    if base_model_id.startswith("ministral-"):
        return "ministral"

    return "mistral"


def _preferred_key_label(preferred_model: str) -> str:
    lower = preferred_model.lower()
    if "|key3" in lower:
        return "Key 3"
    if "|key2" in lower:
        return "Key 2"
    return "Key 1"


# -----------------------------------------------------------------------------
# FINALIZATION / TELEMETRY
# -----------------------------------------------------------------------------
def _finalize(
    result: dict,
    provider: str,
    model_id: str,
    r_type: str,
    circuit_key: str | None = None,
) -> dict:
    if sc.supabase and result.get("speed") is not None:
        async def _log_usage():
            try:
                await asyncio.to_thread(
                    lambda: sc.supabase.table("llm_usage").insert({
                        "request_type": r_type,
                        "provider": provider,
                        "model": model_id,
                        "success": True,
                        "speed_secs": result["speed"],
                    }).execute()
                )
            except Exception:
                # Telemetry is never allowed to fail the user's request.
                pass

        asyncio.create_task(_log_usage())

    return {
        "success": True,
        "text": result["text"],
        "_model_used": model_id,
        "_circuit_key": circuit_key,
    }


async def _log_all_failed():
    if not sc.supabase:
        return

    try:
        await asyncio.to_thread(
            lambda: sc.supabase.table("llm_usage").insert({
                "request_type": "all_failed",
                "provider": "all_failed",
                "model": "all_failed",
                "success": False,
                "speed_secs": 0,
            }).execute()
        )
    except Exception:
        pass


# -----------------------------------------------------------------------------
# MAIN FALLBACK ROUTER
# -----------------------------------------------------------------------------
async def call_llm_balanced(
    prompt: str,
    is_r1: bool,
    preferred_model: str = "",
    no_ai_changes: bool = False,
) -> dict:
    """
    Reliable multi-provider router.

    Important behavior:
      * A circuit belongs to provider + model + API key.
      * One failed model/key never blocks another model/key.
      * Provider exceptions and timeouts fall through to the next candidate.
      * If every candidate is temporarily circuit-tripped, the router performs
        a recovery pass that ignores the breaker and tries them again.
      * A successful candidate becomes the next round-robin starting point.
    """
    async with _LLM_SEMAPHORE:
        max_tokens = _R1_MAX_TOKENS if is_r1 else _R2_MAX_TOKENS
        all_attempts = []

        # ------------------------------------------------------------------
        # Load persisted model-specific circuit breakers.
        # ------------------------------------------------------------------
        db_tripped_keys = set()
        if sc.supabase:
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: sc.supabase.rpc("get_tripped_circuits").execute()
                    ),
                    timeout=0.8,
                )
                if res.data:
                    db_tripped_keys = {
                        row["circuit_key"]
                        for row in res.data
                        if row.get("circuit_key")
                    }
            except Exception:
                # If Supabase is temporarily unavailable, local routing continues.
                pass

        # ------------------------------------------------------------------
        # Build the fallback chain.
        # ------------------------------------------------------------------
        chain = []
        is_explicit_preferred = bool(
            preferred_model and preferred_model != "auto"
        )
        is_fastest_preferred = preferred_model in ("fastest", "low_latency")

        if is_fastest_preferred:
            # Fast path still has multiple providers. If one fails, continue.
            chain = [
                (
                    "cloudflare",
                    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                    "Key 1",
                ),
                ("mistral", "mistral-medium-3-5", "Key 1"),
                ("deepseek", "deepseek-v4-flash", "Key 1"),
                ("POOL", 2, None),
            ]
            is_explicit_preferred = False
        elif is_explicit_preferred:
            provider = _get_provider_for_model(preferred_model)
            base_model_id = preferred_model.split("|")[0]
            key_label = _preferred_key_label(preferred_model)
            chain.append((provider, base_model_id, key_label))
        else:
            # Universal fallback: DeepSeek first, then both pools.
            chain = [
                ("deepseek", "deepseek-v4-flash", "Key 1"),
                ("POOL", 1, None),
                ("POOL", 2, None),
            ]

        # ------------------------------------------------------------------
        # Build the exact candidate circuit keys before making requests.
        # ------------------------------------------------------------------
        candidates = []
        for item in chain:
            if item[0] == "POOL":
                models = await _get_pool_models(item[1])
            else:
                models = [item]

            for provider, model_id, key_label in models:
                candidates.append(
                    _make_circuit_key(provider, model_id, key_label)
                )

        # If every candidate is blocked, do a recovery pass rather than
        # returning circuit_breaker_active for every model.
        all_tripped = bool(candidates) and all(
            _is_tripped(key, db_tripped_keys) for key in candidates
        )

        if all_tripped:
            print(
                "[LLM] All candidates are circuit-tripped. "
                "Starting recovery pass."
            )

        # ------------------------------------------------------------------
        # Execute every candidate in order until one succeeds.
        # ------------------------------------------------------------------
        for item in chain:
            original_pool = None
            pool_type = None

            if item[0] == "POOL":
                pool_type = item[1]
                models = await _get_pool_models(pool_type)
                original_pool = POOL_1 if pool_type == 1 else POOL_2
            else:
                models = [item]

            for provider, model_id, key_label in models:
                circuit_key = _make_circuit_key(provider, model_id, key_label)

                # In recovery mode, deliberately ignore the breaker and retry.
                if not all_tripped and not is_explicit_preferred:
                    if _is_tripped(circuit_key, db_tripped_keys):
                        all_attempts.append({
                            "model": f"{model_id} - {key_label}",
                            "status": "circuit_breaker_active",
                        })
                        continue

                if is_explicit_preferred and _is_tripped(
                    circuit_key, db_tripped_keys
                ):
                    # Explicit selection should still be usable after a transient
                    # failure; retry it rather than hiding the user's choice.
                    print(
                        f"[LLM] Explicit model {circuit_key} is tripped; retrying"
                    )

                caller = _CALLERS.get((provider, key_label))
                if caller is None:
                    all_attempts.append({
                        "model": f"{model_id} - {key_label}",
                        "status": "caller_not_configured",
                    })
                    continue

                try:
                    result = await asyncio.wait_for(
                        caller(model_id, prompt, max_tokens),
                        timeout=_LLM_CALL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    status = f"provider_timeout_{_LLM_CALL_TIMEOUT}s"
                    print(f"[LLM] {circuit_key}: {status}; trying next")
                    all_attempts.append({
                        "model": f"{model_id} - {key_label}",
                        "status": status,
                    })
                    # A timeout is not permanently bad, so don't blacklist it.
                    continue
                except Exception as exc:
                    status = f"provider_exception: {type(exc).__name__}: {exc}"
                    print(f"[LLM] {circuit_key}: {status}; trying next")
                    all_attempts.append({
                        "model": f"{model_id} - {key_label}",
                        "status": status,
                    })
                    continue

                if not isinstance(result, dict):
                    all_attempts.append({
                        "model": f"{model_id} - {key_label}",
                        "status": "invalid_provider_response",
                    })
                    continue

                if result.get("success"):
                    print(
                        f"[LLM Fallback] Attempted {len(all_attempts) + 1} "
                        f"model(s) -> Winner: {model_id} - {key_label} "
                        f"({result.get('speed', 'N/A')}s)"
                    )

                    if original_pool is not None:
                        try:
                            winner_idx = original_pool.index(
                                (provider, model_id, key_label)
                            )
                            await _advance_rr_index(
                                pool_type,
                                len(original_pool),
                                winner_idx,
                            )
                        except ValueError:
                            pass

                    # A successful call proves this exact combination works.
                    await _reset_circuit(circuit_key)

                    return _finalize(
                        result,
                        provider,
                        model_id,
                        "r1" if is_r1 else "r2",
                        circuit_key,
                    )

                attempts = result.get("attempts", []) or []
                err_type = _get_rate_limit_type(attempts)

                if err_type:
                    await _trip_circuit(circuit_key, err_type)

                if attempts:
                    for att in attempts:
                        if isinstance(att, dict):
                            att["model"] = f"{model_id} - {key_label}"
                    all_attempts.extend(attempts)
                else:
                    all_attempts.append({
                        "model": f"{model_id} - {key_label}",
                        "status": "provider_failed_without_attempt_details",
                    })

        # ------------------------------------------------------------------
        # Nothing succeeded.
        # ------------------------------------------------------------------
        asyncio.create_task(_log_all_failed())

        return {
            "success": False,
            "all_attempts": all_attempts,
        }


# -----------------------------------------------------------------------------
# PUBLIC API USED BY THE REST ROUTERS
# -----------------------------------------------------------------------------
async def call_llm_r1(prompt: str, preferred_model: str = "") -> dict:
    try:
        return await asyncio.wait_for(
            call_llm_balanced(prompt, True, preferred_model),
            timeout=_LLM_TOTAL_TIMEOUT_R1,
        )
    except asyncio.TimeoutError:
        return {
            "success": False,
            "timeout": True,
            "all_attempts": [{
                "model": "all",
                "status": f"llm_global_timeout_{_LLM_TOTAL_TIMEOUT_R1}s",
            }],
        }
    except Exception as exc:
        # Never let an unexpected router exception crash FastAPI with no details.
        return {
            "success": False,
            "all_attempts": [{
                "model": "router",
                "status": f"router_exception: {type(exc).__name__}: {exc}",
            }],
        }


async def call_llm_r2(
    prompt: str,
    preferred_model: str = "",
    no_ai_changes: bool = False,
) -> dict:
    try:
        return await asyncio.wait_for(
            call_llm_balanced(
                prompt,
                False,
                preferred_model,
                no_ai_changes,
            ),
            timeout=_LLM_TOTAL_TIMEOUT_R2,
        )
    except asyncio.TimeoutError:
        return {
            "success": False,
            "timeout": True,
            "all_attempts": [{
                "model": "all",
                "status": f"llm_global_timeout_{_LLM_TOTAL_TIMEOUT_R2}s",
            }],
        }
    except Exception as exc:
        return {
            "success": False,
            "all_attempts": [{
                "model": "router",
                "status": f"router_exception: {type(exc).__name__}: {exc}",
            }],
        }