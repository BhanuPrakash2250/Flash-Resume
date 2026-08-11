-- Create system_metrics table for tracking peak concurrent users
CREATE TABLE IF NOT EXISTS public.system_metrics (
  id TEXT PRIMARY KEY,
  value JSONB NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Enable Row Level Security for system_metrics
ALTER TABLE public.system_metrics ENABLE ROW LEVEL SECURITY;

-- Create rr_counters table for Round-Robin routing (with correct schema)
CREATE TABLE IF NOT EXISTS public.rr_counters (
  name TEXT PRIMARY KEY,
  counter INTEGER DEFAULT 0,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Enable Row Level Security for rr_counters
ALTER TABLE public.rr_counters ENABLE ROW LEVEL SECURITY;

-- Insert initial data for rr_counters
INSERT INTO public.rr_counters (name, counter)
VALUES ('pool_1_global', 0), ('pool_2_global', 0)
ON CONFLICT (name) DO NOTHING;

-- Insert initial data for system_metrics
INSERT INTO public.system_metrics (id, value)
VALUES ('peak_concurrent_users', '{"count": 0, "timestamp": null}')
ON CONFLICT (id) DO NOTHING;