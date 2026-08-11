from fastapi import APIRouter, HTTPException, Request, Header
import asyncio
import hmac
from pydantic import BaseModel
import os
import random
import httpx
from dotenv import load_dotenv
import json
import supabase_client as sc
from supabase_client import sb
from datetime import datetime, timedelta, timezone

load_dotenv(override=True)

router = APIRouter()
from rate_limiter import limiter

class OrderRequest(BaseModel):
    amount: int | None = None  # Deprecated: client amounts are ignored
    plan_type: str
    user_id: str
    email: str = None
    affiliate_code: str | None = None  # ref cookie value from frontend

@router.post("/payments/create-order")
@limiter.limit("10/minute")
async def create_order(request: Request, body: OrderRequest, authorization: str = Header(None)):
    PRICES = {
        "pay_per_use": 2900,
        "regular": 19900,
        "bulk_offer": 59900,
        "student": 9900
    }
    amount_in_paise = PRICES.get(body.plan_type)
    if not amount_in_paise:
        raise HTTPException(status_code=400, detail="Invalid plan type")

    # Ensure user exists in public.users to prevent foreign key constraint violations
    if sc.supabase and body.email:
        try:
            # Check if user exists
            user_check = await sb(lambda: sc.supabase.table("users").select("id").eq("id", body.user_id).execute())
            if not user_check.data:
                # Insert missing user record
                await sb(lambda: sc.supabase.table("users").insert({
                    "id": body.user_id,
                    "email": body.email
                }).execute())
        except Exception as e:
            print(f"Failed to ensure user exists in public.users: {e}")
            # Continue anyway, let it fail at payments insert if it must

    # Return a mock response since payment processing is disabled
    return {
        "razorpay_order_id": "mock_order_id",
        "amount": amount_in_paise,
        "currency": "INR"
    }

class VerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    user_id: str | None = None  # Deprecated: backend uses DB user_id
    plan_type: str | None = None # Deprecated: backend uses DB plan_type
    amount: int | None = None   # Deprecated
    session_id: str | None = None

@router.post("/payments/verify")
async def verify_payment(body: VerifyRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    try:
        token = authorization.split(" ")[1]
        user_res = await asyncio.to_thread(sc.supabase.auth.get_user, token)
        if not user_res or not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        auth_user_id = user_res.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Return success response since payment processing is disabled
    return {"status": "ok"}

class UpdateStatusRequest(BaseModel):
    razorpay_order_id: str
    status: str
    failure_source: str | None = None
    failure_reason: str | None = None

@router.patch("/payments/update-status")
async def update_payment_status(body: UpdateStatusRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    try:
        token = authorization.split(" ")[1]
        user_res = await asyncio.to_thread(sc.supabase.auth.get_user, token)
        if not user_res or not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        auth_user_id = user_res.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not sc.supabase:
        return {"status": "ignored"}

    if body.status not in {"abandoned", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid status value")

    # Ownership check
    payment_res = await sb(
        lambda: sc.supabase.table("payments").select("user_id")
        .eq("razorpay_order_id", body.razorpay_order_id)
        .execute()
    )
    if not payment_res.data:
        raise HTTPException(status_code=404, detail="Order not found")
    if payment_res.data[0]["user_id"] != auth_user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    update_data = {
        "status": body.status
    }
    if body.status in ("failed", "abandoned"):
        update_data["failed_at"] = datetime.now(timezone.utc).isoformat()
        if body.failure_source:
            update_data["failure_source"] = body.failure_source
        if body.failure_reason:
            update_data["failure_reason"] = body.failure_reason

    await sb(
        lambda: sc.supabase.table("payments").update(update_data)
        .eq("razorpay_order_id", body.razorpay_order_id)
        .execute()
    )
    return {"status": "ok"}

class DeductRequest(BaseModel):
    user_id: str
    session_id: str | None = None

@router.post("/payments/deduct-credit")
async def deduct_credit(body: DeductRequest, authorization: str = Header(None)):
    if not sc.supabase:
        raise HTTPException(status_code=500, detail="Database not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    try:
        token = authorization.split(" ")[1]
        user_res = await asyncio.to_thread(sc.supabase.auth.get_user, token)
        if not user_res or not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        if user_res.user.id != body.user_id:
            raise HTTPException(status_code=403, detail="Not authorized for this user")
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Return success response since payment processing is disabled
    return {"status": "success", "new_balance": 100}

# ── OTP Routes ──────────────────────────────────────────────
# NOTE: We use Brevo's HTTP API (not SMTP) because Render free tier
# blocks all outbound SMTP ports (25, 465, 587). HTTP API uses port 443.

BREVO_API_KEY = os.getenv("BREVO_API_KEY")        # Brevo API key (not SMTP key)
BREVO_FROM_EMAIL = os.getenv("BREVO_FROM_EMAIL", "flashresume.in@gmail.com")
BREVO_FROM_NAME = os.getenv("BREVO_FROM_NAME", "Flashresume")

class SendOtpRequest(BaseModel):
    email: str

@router.post("/payments/send-otp")
@limiter.limit("3/minute")
async def send_otp(request: Request, body: SendOtpRequest):
    if not sc.supabase:
        raise HTTPException(status_code=500, detail="Database not configured")
    if not BREVO_API_KEY:
        raise HTTPException(status_code=500, detail="Email service not configured")

    email = body.email.strip().lower()
    otp_code = str(random.randint(100000, 999999))
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()

    # Upsert OTP — reset failed_attempts so previously locked users can retry
    try:
        await sb(lambda: sc.supabase.table("otp_verifications").upsert({
            "email": email,
            "otp": otp_code,
            "expires_at": expires_at,
            "verified": False,
            "failed_attempts": 0
        }, on_conflict="email").execute())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")

    # Send email via Brevo HTTP API (port 443 — works on Render free tier)
    html_body = f"""
    <div style="font-family: Inter, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px;">
      <h2 style="color: #006859; font-size: 24px; margin-bottom: 8px;">Your Verification Code</h2>
      <p style="color: #595c5d; font-size: 14px;">Use this code to unlock the Student Plan on Flashresume:</p>
      <div style="background: #f5f6f7; border-radius: 16px; padding: 32px; text-align: center; margin: 24px 0;">
        <span style="font-size: 40px; font-weight: 900; letter-spacing: 12px; color: #006859;">{otp_code}</span>
      </div>
      <p style="color: #595c5d; font-size: 12px;">This code expires in <strong>10 minutes</strong>. Do not share it with anyone.</p>
      <hr style="border: none; border-top: 1px solid #eff1f2; margin: 24px 0;" />
      <p style="color: #595c5d; font-size: 11px;">Flashresume &mdash; AI-Powered Resume Optimization</p>
    </div>
    """
    payload = {
        "sender": {"name": BREVO_FROM_NAME, "email": BREVO_FROM_EMAIL},
        "to": [{"email": email}],
        "subject": "Your Flashresume Verification Code",
        "htmlContent": html_body
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={
                    "api-key": BREVO_API_KEY,
                    "Content-Type": "application/json"
                }
            )
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail=f"Email API error: {resp.text}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=500, detail="Email service timed out. Please try again.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")

    return {"status": "ok", "message": "OTP sent"}

class VerifyOtpRequest(BaseModel):
    email: str
    otp: str

@router.post("/payments/verify-otp")
@limiter.limit("5/minute")
async def verify_otp(request: Request, body: VerifyOtpRequest):
    if not sc.supabase:
        raise HTTPException(status_code=500, detail="Database not configured")

    email = body.email.strip().lower()

    record_res = await sb(lambda: sc.supabase.table("otp_verifications") \
        .select("otp, expires_at, failed_attempts") \
        .eq("email", email).single().execute())

    if not record_res.data:
        raise HTTPException(404, "No OTP found for this email. Please request a new one.")

    record = record_res.data

    if record.get("failed_attempts", 0) >= 5:
        raise HTTPException(429, "Too many failed attempts. Request a new OTP.")

    if datetime.fromisoformat(record["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(400, "OTP expired")

    if not hmac.compare_digest(str(record["otp"]).strip(), str(body.otp).strip()):
        # Increment failed counter
        await sb(lambda: sc.supabase.table("otp_verifications") \
            .update({"failed_attempts": record.get("failed_attempts", 0) + 1}) \
            .eq("email", email).execute())
        raise HTTPException(400, "Invalid OTP")

    # Success — clean up
    await sb(lambda: sc.supabase.table("otp_verifications").delete().eq("email", email).execute())
    return {"status": "ok", "verified": True}