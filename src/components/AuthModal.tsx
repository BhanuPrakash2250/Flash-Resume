"use client";

import { motion } from "motion/react";
import { X } from "lucide-react";

interface AuthModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

export default function AuthModal({ isOpen, onClose }: AuthModalProps) {
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-[100] flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        className="bg-surface rounded-3xl w-full max-w-md shadow-2xl overflow-hidden relative border border-surface-container-high p-8"
      >
        <button
          onClick={onClose}
          aria-label="Close"
          className="absolute top-4 right-4 p-2 bg-surface-container-low hover:bg-surface-container-high rounded-full transition-colors"
        >
          <X className="w-5 h-5 text-on-surface-variant" />
        </button>

        <div className="text-center">
          <h2 className="text-2xl font-headline font-bold text-on-background mb-2">
            Welcome to Flashresume
          </h2>
          <p className="text-sm text-on-surface-variant leading-relaxed">
            Sign-in has been removed. You can create, optimize, and download resumes without a Google account.
            Account credits, referrals, and paid plans are unavailable until an account service is configured.
          </p>
          <button
            onClick={onClose}
            className="mt-6 w-full bg-primary text-white font-bold py-3.5 rounded-xl hover:opacity-90 transition-opacity"
          >
            Continue
          </button>
        </div>
      </motion.div>
    </div>
  );
}
