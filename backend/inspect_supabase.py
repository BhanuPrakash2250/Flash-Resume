import supabase_client as sc
from supabase_client import sb
import asyncio

async def main():
    print('supabase', type(sc.supabase), sc.supabase is None)
    def query():
        return sc.supabase.table('feedback').select('rating').limit(1).execute()
    try:
        res = await sb(query, fallback=[])
        print('res type', type(res))
        print('has data', hasattr(res, 'data'))
        if hasattr(res, 'data'):
            print('data attr type', type(res.data))
            print('data repr', repr(res.data))
        print('count attr', getattr(res, 'count', None))
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())
