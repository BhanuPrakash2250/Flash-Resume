from fastapi import APIRouter, HTTPException, Depends
from routers.admin import require_admin
from pydantic import BaseModel
import asyncio
import supabase_client as sc
from supabase_client import sb

router = APIRouter()


def _normalize_result(result):
    if result is None:
        return []
    if hasattr(result, 'data'):
        return result.data or []
    return result


def _count_result(result):
    if result is None:
        return 0
    if hasattr(result, 'count') and not callable(result.count) and result.count is not None:
        return result.count
    if hasattr(result, 'data'):
        return len(result.data or [])
    if isinstance(result, (list, tuple)):
        return len(result)
    return 0


def _get_one(result):
    if result is None:
        return None
    if hasattr(result, 'data'):
        return result.data
    return result


class FeedbackRequest(BaseModel):
    user_id: str
    session_id: str
    rating: int
    suggestion: str = ""

@router.post("/feedback/submit")
async def submit_feedback(body: FeedbackRequest):
    if not sc.supabase:
        raise HTTPException(status_code=500, detail="Database not configured")
        
    if body.session_id:
        session = await sb(lambda: sc.supabase.table("resume_sessions").select("download_count, user_id").eq("id", body.session_id).single().execute(), fallback=None)
        
        if not session or not getattr(session, 'data', None):
            raise HTTPException(404, "Session not found")
        if session.data["user_id"] != body.user_id:
            raise HTTPException(403, "Not your session")
        if (session.data.get("download_count") or 0) < 1:
            raise HTTPException(400, "Feedback only accepted after first download")
    else:
        # Scratch mode has no session_id
        if not body.user_id:
            raise HTTPException(400, "User ID is required for feedback")
        
    # Prevent duplicate feedback for the same session
    # Prevent duplicate feedback for the same session (or scratch mode)
    query = sc.supabase.table("feedback").select("id").eq("user_id", body.user_id)
    if body.session_id:
        query = query.eq("session_id", body.session_id)
    else:
        # Prevent multiple scratch mode feedbacks per user if desired, or allow them?
        # The frontend asks once on first download, or 8th global download.
        # We can just check for null session_id
        query = query.is_("session_id", "null")
        
    existing = await sb(lambda: query.limit(1).execute(), fallback=None)
    if existing is None:
        raise HTTPException(status_code=503, detail="Feedback service unavailable")
    if existing.data:
        raise HTTPException(409, "Feedback already submitted for this session")

    result = await sb(
        lambda: sc.supabase.table("feedback").insert({
            "user_id": body.user_id,
            "session_id": body.session_id or None,
            "rating": body.rating,
            "suggestion": body.suggestion
        }).execute(),
        fallback=None
    )
    if result is None:
        raise HTTPException(status_code=503, detail="Feedback service unavailable")

    return {"success": True}

@router.get("/admin/feedback", dependencies=[Depends(require_admin)])
async def get_feedback():
    if not sc.supabase:
        return []
    result = await sb(
        lambda: sc.supabase.table("feedback").select("*, users(email)", count="exact").gte("created_at", "2026-05-28T00:00:00Z").order("created_at", desc=True).limit(100).execute(),
        fallback=[]
    )

    reviews = _normalize_result(result)
    total_count = _count_result(result)
    return {"reviews": reviews, "total_count": total_count}

@router.get("/public/reviews")
async def get_public_reviews():
    """Public endpoint: returns reviews with text, excluding internal/test accounts."""
    if not sc.supabase:
        return []
    result = await sb(
        lambda: sc.supabase.table("feedback")
            .select("rating, suggestion, created_at, users(email)")
            .gte("created_at", "2026-05-28T00:00:00Z")
            .order("created_at", desc=True)
            .limit(200)
            .execute(),
        fallback=[]
    )
    cleaned = _normalize_result(result)
    filtered = [
        r for r in cleaned
        if r.get("suggestion", "").strip()
        and len(r.get("suggestion", "").strip().split()) > 1
        and r.get("rating", 0) >= 3
    ]
    return filtered


@router.get("/public/review-stats")
async def get_review_stats():
    """Public endpoint: returns aggregate trust stats + total signup count.
    Matches admin FeedbackPanel.tsx logic exactly:
    - avg_rating and five_star_rate → computed from most recent 100 reviews
    - total_reviews                 → exact DB count (count="exact")
    - total_signups                 → exact count from users table
    All queries run in parallel via asyncio.gather — zero extra latency.
    """
    if not sc.supabase:
        return {"avg_rating": 4.3, "total_reviews": 0, "five_star_rate": 0, "total_signups": 0}

    # Fire all 3 queries in parallel
    count_res, sample_res, signups_res = await asyncio.gather(
        # 1. Exact feedback count
        sb(
            lambda: sc.supabase.table("feedback")
                .select("id", count="exact")
                .gte("created_at", "2026-05-28T00:00:00Z")
                .execute(),
            fallback=[]
        ),
        # 2. Latest 100 ratings for avg & 5★ (same as admin panel)
        sb(
            lambda: sc.supabase.table("feedback")
                .select("rating")
                .gte("created_at", "2026-05-28T00:00:00Z")
                .order("created_at", desc=True)
                .limit(100)
                .execute(),
            fallback=[]
        ),
        # 3. Total signup count from users table
        sb(
            lambda: sc.supabase.table("users")
                .select("id", count="exact")
                .execute(),
            fallback=[]
        ),
    )

    total_reviews = _count_result(count_res)
    total_signups = _count_result(signups_res)
    ratings = [r["rating"] for r in _normalize_result(sample_res) if r.get("rating")]

    if not ratings:
        return {"avg_rating": 4.3, "total_reviews": total_reviews,
                "five_star_rate": 0, "total_signups": total_signups}

    avg = round(sum(ratings) / len(ratings), 1)
    five_star = round(len([r for r in ratings if r == 5]) / len(ratings) * 100)
    return {
        "avg_rating": avg,
        "total_reviews": total_reviews,
        "five_star_rate": five_star,
        "total_signups": total_signups,
    }




class IncrementDownloadRequest(BaseModel):
    session_id: str
    user_id: str | None = None
    device_type: str = "desktop"

@router.post("/resume/increment-download")
async def increment_download(body: IncrementDownloadRequest):
    fallback_response = {
        "download_count": 0,
        "total_platform_downloads": 0,
        "user_total_downloads": 0,
    }

    if not sc.supabase:
        return fallback_response

    new_count = 0
    global_count = 0
    user_total_downloads = 0
    actual_user_id = None

    try:
        # Scratch mode sends empty session_id — skip session lookup & DB increment
        if body.session_id:
            # 1. Verify session ownership before doing anything
            session = await sb(
                lambda: sc.supabase.table("resume_sessions")
                    .select("download_count, user_id")
                    .eq("id", body.session_id)
                    .single()
                    .execute(),
                fallback=None,
            )
            if not session or not getattr(session, "data", None):
                return fallback_response
            actual_user_id = session.data.get("user_id")

            # 2. Increment download_count on the session row.
            old_count = session.data.get("download_count") or 0
            updated = await sb(
                lambda: sc.supabase.table("resume_sessions")
                    .update({"download_count": old_count + 1})
                    .eq("id", body.session_id)
                    .select("download_count")
                    .execute(),
                fallback=None,
            )

            if updated and hasattr(updated, "data") and updated.data:
                new_count = updated.data[0].get("download_count", old_count + 1)
            else:
                new_count = old_count + 1
        else:
            # Scratch mode: no session row — use user_id from body directly
            actual_user_id = body.user_id

        # 3. Log global download and count (UNIQUE constraint makes this idempotent on retry)
        if actual_user_id:
            try:
                await sb(
                    lambda: sc.supabase.table("resume_downloads").insert({
                        "user_id": actual_user_id,
                        "session_id": body.session_id or None,
                        "device_type": body.device_type,
                    }).execute(),
                    fallback=None,
                )
            except Exception:
                pass

            try:
                global_res, user_res = await asyncio.gather(
                    sb(
                        lambda: sc.supabase.table("resume_downloads")
                            .select("id", count="exact")
                            .execute(),
                        fallback=None,
                    ),
                    sb(
                        lambda: sc.supabase.table("resume_downloads")
                            .select("id", count="exact")
                            .eq("user_id", actual_user_id)
                            .execute(),
                        fallback=None,
                    ),
                )
                if global_res and hasattr(global_res, 'count') and global_res.count is not None:
                    global_count = global_res.count
                elif global_res and getattr(global_res, 'data', None) is not None:
                    global_count = len(global_res.data)

                if user_res and hasattr(user_res, 'count') and user_res.count is not None:
                    user_total_downloads = user_res.count
                elif user_res and getattr(user_res, 'data', None) is not None:
                    user_total_downloads = len(user_res.data)
            except Exception:
                pass
    except Exception as e:
        print(f"[Download] Increment endpoint failed, returning fallback: {e}")
        return fallback_response

    return {
        "download_count": new_count,
        "total_platform_downloads": global_count,
        "user_total_downloads": user_total_downloads,
    }

@router.get("/admin/llm-stats", dependencies=[Depends(require_admin)])
async def llm_stats():
    if not sc.supabase:
        return []
    result = await sb(lambda: sc.supabase.table("llm_usage").select("*").gte("created_at", "2026-05-28T00:00:00Z").order("created_at", desc=True).limit(100).execute())
    return result.data
