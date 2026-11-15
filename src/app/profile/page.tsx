export default function ProfileUnavailablePage() {
  return (
    <main className="min-h-screen bg-[#0f1117] flex items-center justify-center px-6 py-12 font-sans">
      <div className="max-w-lg w-full rounded-3xl border border-white/10 bg-white/5 p-8 text-center shadow-2xl">
        <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-400/10 text-amber-300">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-7 w-7">
            <circle cx="12" cy="8" r="3.5" />
            <path d="M5 20c.7-3.4 3.1-5.2 7-5.2s6.3 1.8 7 5.2" strokeLinecap="round" />
          </svg>
        </div>
        <h1 className="mb-3 text-2xl font-bold text-white">Account features unavailable</h1>
        <p className="mb-7 text-sm leading-6 text-white/70">
          Sign-in has been removed. Profile details, saved account data, credits and application tracking are unavailable.
          You can still use the public resume creation and AI optimization tools.
        </p>
        <a href="/" className="inline-flex rounded-xl bg-[#006859] px-6 py-3 text-sm font-bold text-white transition hover:opacity-90">
          Back to Flashresume
        </a>
      </div>
    </main>
  );
}
