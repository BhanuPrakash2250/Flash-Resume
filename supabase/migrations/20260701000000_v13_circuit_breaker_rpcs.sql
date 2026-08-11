-- v13: Circuit breaker RPCs + missing payment columns
-- NOTE: Live DB has already been manually restructured.

-- 1. Add 3 missing payment failure columns (failed_at already exists)
ALTER TABLE public.payments
  ADD COLUMN IF NOT EXISTS failure_reason TEXT,
  ADD COLUMN IF NOT EXISTS failure_code   TEXT,
  ADD COLUMN IF NOT EXISTS failure_source TEXT;

-- 2. Ensure llm_circuit_breakers has the required key columns
ALTER TABLE public.llm_circuit_breakers
  ADD COLUMN IF NOT EXISTS circuit_key TEXT,
  ADD COLUMN IF NOT EXISTS tripped_until TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_circuit_breakers_circuit_key
  ON public.llm_circuit_breakers (circuit_key);

ALTER TABLE public.llm_circuit_breakers
  DROP CONSTRAINT IF EXISTS llm_circuit_breakers_pkey;

ALTER TABLE public.llm_circuit_breakers
  ADD CONSTRAINT IF NOT EXISTS llm_circuit_breakers_circuit_key_pkey PRIMARY KEY (circuit_key);

-- 3. trip_circuit_breaker RPC
CREATE OR REPLACE FUNCTION public.trip_circuit_breaker(
  p_circuit_key      TEXT,
  p_cooldown_seconds INT
)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  INSERT INTO public.llm_circuit_breakers (circuit_key, tripped_until)
  VALUES (
    p_circuit_key,
    NOW() + (p_cooldown_seconds || ' seconds')::INTERVAL
  )
  ON CONFLICT (circuit_key) DO UPDATE
    SET tripped_until = NOW() + (p_cooldown_seconds || ' seconds')::INTERVAL;
END;
$$;

-- 4. reset_circuit_breaker RPC
CREATE OR REPLACE FUNCTION public.reset_circuit_breaker(
  p_circuit_key TEXT
)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  DELETE FROM public.llm_circuit_breakers
  WHERE circuit_key = p_circuit_key;
END;
$$;

-- 5. get_tripped_circuits RPC
CREATE OR REPLACE FUNCTION public.get_tripped_circuits()
RETURNS TABLE(circuit_key TEXT) LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  RETURN QUERY
  SELECT lcb.circuit_key
  FROM public.llm_circuit_breakers lcb
  WHERE lcb.tripped_until > NOW()
    AND lcb.circuit_key IS NOT NULL;
END;
$$;

-- 4. Ensure rr_counters seed rows exist
INSERT INTO public.rr_counters (name, counter)
VALUES ('pool_1_global', 0), ('pool_2_global', 0)
ON CONFLICT (name) DO NOTHING;
