
#!/usr/bin/env python3

"""
Script to create missing database tables for FlashResume.
This script creates the system_metrics and rr_counters tables that are missing.
"""

import os
import sys
from dotenv import load_dotenv
import supabase
from supabase import create_client

# Load environment variables
load_dotenv()

def get_supabase_client():
    """Create and return a Supabase client."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")

    if not url or not key:
        print("Error: Missing Supabase environment variables")
        print(f"SUPABASE_URL: {url}")
        print(f"SUPABASE_SERVICE_ROLE_KEY: {os.getenv('SUPABASE_SERVICE_ROLE_KEY')}")
        print(f"SUPABASE_ANON_KEY: {os.getenv('SUPABASE_ANON_KEY')}")
        sys.exit(1)

    return create_client(url, key)

def create_missing_tables():
    """Create the missing database tables."""
    try:
        supabase_client = get_supabase_client()

        print("Creating missing database tables...")

        # Create system_metrics table
        print("Creating system_metrics table...")
        supabase_client.table("system_metrics").upsert({
            "id": "peak_concurrent_users",
            "value": {"count": 0, "timestamp": None}
        }).execute()

        # Create rr_counters table with correct schema
        print("Creating rr_counters table...")
        # First try to insert the expected data
        try:
            supabase_client.table("rr_counters").upsert({
                "name": "pool_1_global",
                "counter": 0
            }).execute()

            supabase_client.table("rr_counters").upsert({
                "name": "pool_2_global",
                "counter": 0
            }).execute()
        except Exception as e:
            print(f"Error creating rr_counters with name/counter: {e}")
            # Try alternative schema if the first one fails
            try:
                supabase_client.table("rr_counters").upsert({
                    "id": "pool_1_global",
                    "current_index": 0
                }).execute()

                supabase_client.table("rr_counters").upsert({
                    "id": "pool_2_global",
                    "current_index": 0
                }).execute()
            except Exception as e2:
                print(f"Error creating rr_counters with id/current_index: {e2}")
                return False

        print("✅ Tables created successfully!")
        return True

    except Exception as e:
        print(f"❌ Error creating tables: {e}")
        return False

if __name__ == "__main__":
    success = create_missing_tables()
    sys.exit(0 if success else 1)