import { NextResponse } from "next/server";

export async function GET() {
  const backendUrl = process.env.NEXT_PUBLIC_API_URL || "https://flash-resume.onrender.com";
  try {
    const res = await fetch(`${backendUrl}/api/public/reviews`, {
      cache: "no-store",
    });
    if (!res.ok) return NextResponse.json([], { status: 200 });
    const data = await res.json();
    return NextResponse.json(data);
  } catch {
    return NextResponse.json([]);
  }
}

