"use client";

interface PricingPopupProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
  initialPlan?: string | null;
  directPay?: boolean;
  forcePlanSelect?: boolean;
  prefetchedUser?: any;
  prefetchedCredits?: number;
  disableClose?: boolean;
  loginOnly?: boolean;
}

/**
 * Payments have been removed from FlashResume.
 * This component remains as a compatibility wrapper for existing call sites.
 */
export default function PricingPopup({ isOpen, onClose }: PricingPopupProps) {
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-3xl border border-surface-container-high bg-surface p-7 text-center shadow-2xl">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 text-primary">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-7 w-7">
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 12.5 9.5 17 19 7" />
          </svg>
        </div>
        <h2 className="mb-2 text-2xl font-bold text-on-background">FlashResume is free</h2>
        <p className="mb-6 text-sm leading-6 text-on-surface-variant">
          Resume creation, AI optimization, ATS analysis and PDF downloads are available free.
          No payment, credits or subscription is required.
        </p>
        <button
          onClick={onClose}
          className="w-full rounded-xl bg-primary px-5 py-3 font-bold text-white transition-opacity hover:opacity-90"
        >
          Continue for free
        </button>
      </div>
    </div>
  );
}
